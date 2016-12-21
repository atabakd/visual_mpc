""" This file defines an agent for the MuJoCo simulator environment. """
from copy import deepcopy

import numpy as np

import mujoco_py
from mujoco_py.mjlib import mjlib
from mujoco_py.mjtypes import *

import h5py
import cPickle

from PIL import Image

import matplotlib.pyplot as plt

from lsdc.agent.agent import Agent
from lsdc.agent.agent_utils import generate_noise, setup
from lsdc.agent.config import AGENT_MUJOCO
from lsdc.proto.gps_pb2 import JOINT_ANGLES, JOINT_VELOCITIES, \
        END_EFFECTOR_POINTS, END_EFFECTOR_POINT_VELOCITIES, \
        END_EFFECTOR_POINT_JACOBIANS, ACTION, RGB_IMAGE, RGB_IMAGE_SIZE, \
        CONTEXT_IMAGE, CONTEXT_IMAGE_SIZE

from lsdc.sample.sample import Sample

from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from matplotlib.figure import Figure


class AgentMuJoCo(Agent):
    """
    All communication between the algorithms and MuJoCo is done through
    this class.
    """
    def __init__(self, hyperparams):
        config = deepcopy(AGENT_MUJOCO)
        config.update(hyperparams)
        Agent.__init__(self, config)
        self._setup_world(hyperparams['filename'])

        # datastructure for storing all images of a whole sample trajectory;
        self._sample_images = np.zeros((self.T,
                                      self._hyperparams['image_height'],
                                      self._hyperparams['image_width'],
                                      self._hyperparams['image_channels']), dtype= 'uint8')
        self.final_score = None

    def _setup_world(self, filename):
        """
        Helper method for handling setup of the MuJoCo world.
        Args:
            filename: Path to XML file containing the world information.
        """
        self._model = []

        # Initialize Mujoco models. If there's only one xml file, create a single model object,
        # otherwise create a different world for each condition.

        self._model= mujoco_py.MjModel(filename)
        self.model_nomarkers = mujoco_py.MjModel(self._hyperparams['filename_nomarkers'])

         # changes here:
        self._joint_idx = range(self._hyperparams['joint_angles'])
        self._vel_idx = range( self._hyperparams['joint_angles'], self._hyperparams['joint_velocities'] + self._hyperparams['joint_angles'])


        gofast = True
        self._small_viewer = mujoco_py.MjViewer(visible=True,
                                                init_width=self._hyperparams['image_width'],
                                                init_height=self._hyperparams['image_height'],
                                                go_fast=gofast)
        self._small_viewer.start()
        self._small_viewer.cam.camid = 0

        if self._hyperparams['additional_viewer']:
            self._large_viewer = mujoco_py.MjViewer(visible=True, init_width=480,
                                                    init_height=480, go_fast=gofast)
            self._large_viewer.start()

    def finish(self):
        self._large_viewer.finish()
        self._small_viewer.finish()

    def sample(self, policy, verbose=True, save=True, noisy=False):
        """
        Runs a trial and constructs a new sample containing information
        about the trial.
        Args:
            policy: Policy to to used in the trial.
            verbose: Whether or not to plot the trial.
            save: Whether or not to store the trial into the samples.
            noisy: Whether or not to use noise during sampling.
        """

        # Create new sample, populate first time step.
        self._init()

        U = np.empty([self.T, self.dU])
        X_full = np.empty([self.T, 2])
        Xdot_full = np.empty([self.T, 2])

        self._small_viewer.set_model(self.model_nomarkers)

        if self._hyperparams['additional_viewer']:
            self._large_viewer.set_model(self._model)
            self._large_viewer.cam = deepcopy(self._small_viewer.cam)

        # apply action of zero for the first few steps, to let the scene settle
        for t in range(self._hyperparams['skip_first']):
            for _ in range(self._hyperparams['substeps']):
                self._model.data.ctrl = np.array([0. ,0.])
                self._model.step()

        self.large_images_traj = []
        self.large_images = []

        # Take the sample.
        for t in range(self.T):

            X_full[t, :] = self._model.data.qpos[:2].squeeze()
            Xdot_full[t, :] = self._model.data.qvel[:2].squeeze()

            # self.reference_points_show(condition)
            if self._hyperparams['additional_viewer']:
                self._large_viewer.loop_once()

            self._store_image(t)

            if self._hyperparams['data_collection'] or 'random_baseline' in self._hyperparams:
                    mj_U, target_inc = policy.act(X_full[t, :], Xdot_full[t, :], self._sample_images, t)
            else:
                mj_U, pos, ind, targets = policy.act(X_full, Xdot_full, self._sample_images, t, init_model=self._model)
                add_traj = True
                if add_traj:
                    self.large_images_traj += self.add_traj_visual(self.large_images[t], pos, ind, targets)

            if 'poscontroller' in self._hyperparams.keys():
                U[t, :] = target_inc
            else:
                U[t, :] = mj_U

            for _ in range(self._hyperparams['substeps']):
                self._model.data.ctrl = mj_U
                self._model.step()         #simulate the model in mujoco

        if not self._hyperparams['data_collection']:
            self.final_score = self.eval_action()

        if self._hyperparams['record']:
            self.save_gif()

        return X_full, Xdot_full, U, self._sample_images


    def eval_action(self):
        goalpoint = np.array(self._hyperparams['goal_point'])
        refpoint = self._model.data.site_xpos[0,:2]
        return np.linalg.norm(goalpoint - refpoint)

    def _store_image(self,t):
        """
        store image at time index t
        """
        self.model_nomarkers.data.qpos = self._model.data.qpos
        self.model_nomarkers.data.qvel = self._model.data.qvel
        self.model_nomarkers.step()
        self._small_viewer.loop_once()

        img_string, width, height = self._large_viewer.get_image()
        largeimage = np.fromstring(img_string, dtype='uint8').reshape(
                (480, 480, self._hyperparams['image_channels']))[::-1, :, :]

        # import pdb; pdb.set_trace()
        self.large_images.append(largeimage)

        img_string, width, height = self._small_viewer.get_image()
        img = np.fromstring(img_string, dtype='uint8').reshape((height, width, self._hyperparams['image_channels']))[::-1,:,:]

        self._sample_images[t,:,:,:] = img


    def add_traj_visual(self, img, traj, bestindices, targets):

        large_sample_images_traj = []
        fig = plt.figure(figsize=(6, 6), dpi=80)
        fig.add_subplot(111)
        plt.subplots_adjust(left=0, bottom=0, right=1, top=1, wspace=0, hspace=0)

        num_samples = traj.shape[0]
        niter = traj.shape[1]

        for itr in range(niter):

            axes = plt.gca()
            plt.cla()
            axes.axis('off')
            plt.imshow(img, zorder=0)
            axes.autoscale(False)

            for smp in range(num_samples):  # for each trajectory

                x = traj[smp, itr, :, 1]
                y = traj[smp, itr, :, 0]

                if smp == bestindices[itr][0]:
                    plt.plot(x, y, zorder=1, marker='o', color='y')
                elif smp in bestindices[itr][1:]:
                    plt.plot(x, y, zorder=1, marker='o', color='r')
                else:
                    if smp % 5 == 0:
                        plt.plot(x, y, zorder=1, marker='o', color='b')

                # target points #####
                # x = targets[smp, itr, :, 1]
                # y = targets[smp, itr, :, 0]
                #
                # if smp == bestindices[itr][0]:
                #     plt.plot(x, y, zorder=1, marker='o', color='y', linestyle='--')
                # elif smp in bestindices[itr][1:]:
                #     plt.plot(x, y, zorder=1, marker='o', color='r', linestyle='--')
                # else:
                #     if smp % 5 == 0:
                #         plt.plot(x, y, zorder=1, marker='o', color='b', linestyle='--')


            fig.canvas.draw()  # draw the canvas, cache the renderer

            data = np.fromstring(fig.canvas.tostring_rgb(), dtype=np.uint8, sep='')
            data = data.reshape(fig.canvas.get_width_height()[::-1] + (3,))

            large_sample_images_traj.append(data)

        return large_sample_images_traj

    def save_gif(self):
        file_path = self._hyperparams['record']
        from video_prediction.utils_vpred.create_gif import npy_to_gif
        if 'random_baseline' in self._hyperparams:
            npy_to_gif(self.large_images, file_path)
        else:
            npy_to_gif(self.large_images_traj, file_path)

    def _init(self):
        """
        Set the world to a given model, and run kinematics.
        Args:
        """

        #create random starting poses for objects
        def create_pos():
            poses = []
            for i in range(self._hyperparams['num_objects']):
                pos = np.random.uniform(-.35, .35, 2)
                alpha = np.random.uniform(0, np.pi*2)
                ori = np.array([np.cos(alpha/2), 0, 0, np.sin(alpha/2) ])
                poses.append(np.concatenate((pos, np.array([0]), ori), axis= 0))
            return np.concatenate(poses)

        if self._hyperparams['x0'].shape[0] > 4: # if object pose explicit do not sample poses
            object_pos = self._hyperparams['x0'][4:]
        else:
            object_pos= create_pos()

        # Initialize world/run kinematics
        x0 = self._hyperparams['x0']
        if 'goal_point' in self._hyperparams.keys():
            goal = np.append(self._hyperparams['goal_point'], [.1])   # goal point
            ref = np.append(object_pos[:2], [.1]) # reference point
            self._model.data.qpos = np.concatenate((x0[:2], object_pos,goal, ref), 0)
        else:
            self._model.data.qpos = np.concatenate((x0[:2], object_pos), 0)
        self._model.data.qvel = np.zeros_like(self._model.data.qvel)
