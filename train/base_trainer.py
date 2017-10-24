# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import numpy as np
import scipy.signal
import random
import time
import sys
import logging
import six.moves.queue as queue
from collections import namedtuple

from environment.environment import Environment
from model.model import UnrealModel
from train.experience import Experience, ExperienceFrame

logger = logging.getLogger("StRADRL.base_trainer")

LOG_INTERVAL = 1000
PERFORMANCE_LOG_INTERVAL = 1000

Batch = namedtuple("Batch", ["si", "a", "a_r", "adv", "r", "terminal", "features", "pc"])

def process_rollout(rollout, gamma, lambda_=1.0):
    """
    given a rollout, compute its returns and the advantage
    """
    batch_si = np.asarray(rollout.states)
    batch_a = np.asarray(rollout.actions)
    rewards = np.asarray(rollout.rewards)
    action_reward = np.concatenate((batch_a,rewards[:,np.newaxis]), axis=1)
    vpred_t = np.asarray(rollout.values + [rollout.r])

    rewards_plus_v = np.asarray(rollout.rewards + [rollout.r])
    batch_r = discount(rewards_plus_v, gamma)[:-1]
    delta_t = rewards + gamma * vpred_t[1:] - vpred_t[:-1]
    # this formula for the advantage comes "Generalized Advantage Estimation":
    # https://arxiv.org/abs/1506.02438
    batch_adv = discount(delta_t, gamma * lambda_)

    features = rollout.features[0]
    batch_pc = np.asarray(rollout.pixel_changes)
    return Batch(batch_si, batch_a, action_reward, batch_adv, batch_r, rollout.terminal, features, batch_pc)

def discount(x, gamma):
    return scipy.signal.lfilter([1], [1, -gamma], x[::-1], axis=0)[::-1]

class BaseTrainer(object):
    def __init__(self,
               runner,
               global_network,
               initial_learning_rate,
               learning_rate_input,
               grad_applier,
               env_type,
               env_name,
               entropy_beta,
               gamma,
               experience,
               max_global_time_step,
               device):
        self.runner = runner
        self.learning_rate_input = learning_rate_input
        self.env_type = env_type
        self.env_name = env_name
        self.gamma = gamma
        self.max_global_time_step = max_global_time_step
        self.action_size = Environment.get_action_size(env_type, env_name)
        self.local_network = UnrealModel(self.action_size,
                                         0,
                                         entropy_beta,
                                         device)
        self.local_network.prepare_loss()
        
        self.apply_gradients = grad_applier.minimize_local(self.local_network.total_loss,
                                                           global_network.get_vars(),
                                                           self.local_network.get_vars())
        self.sync = self.local_network.sync_from(global_network)
        self.experience = experience
        self.local_t = 0
        self.initial_learning_rate = initial_learning_rate
        self.episode_reward = 0
        # trackers for the experience replay creation
        self.last_action = 0#np.zeros(self.action_size)
        self.last_reward = 0
        
    
    def _anneal_learning_rate(self, global_time_step):
        learning_rate = self.initial_learning_rate * (self.max_global_time_step - global_time_step) / self.max_global_time_step
        if learning_rate < 0.0:
            learning_rate = 0.0
        return learning_rate
        
    def choose_action(self, pi_values):
        return np.random.choice(range(len(pi_values)), p=pi_values)
    
    def set_start_time(self, start_time, global_t):
        self.start_time = start_time
        self.local_t = global_t
        
    def pull_batch_from_queue(self):
        """
        self explanatory:  take a rollout from the queue of the thread runner.
        """
        #@TODO change 100 to a possible variable
        rollout_full = False
        count = 0
        while not rollout_full:
            if count == 0:
                rollout = self.runner.queue.get(timeout=600.0)
                count += 1
            else:
                try:
                    rollout.extend(self.runner.queue.get_nowait())
                    count += 1
                except queue.Empty:
                    #logger.warn("!!! queue was empty !!!")
                    continue
            if count == 5 or rollout.terminal:
                rollout_full = True
        #logger.debug("pulled batch from rollout, length:{}".format(len(rollout.rewards)))
        return rollout
        
    def _print_log(self, global_t):
            elapsed_time = time.time() - self.start_time
            steps_per_sec = global_t / elapsed_time
            logger.info("Performance : {} STEPS in {:.0f} sec. {:.0f} STEPS/sec. {:.2f}M STEPS/hour".format(
            global_t,  elapsed_time, steps_per_sec, steps_per_sec * 3600 / 1000000.))
    
    def _add_batch_to_exp(self, batch):
        #logger.debug("is batch terminal? {}".format(batch.terminal))
        for k in range(len(batch.si)):
            last_action = self.last_action
            last_reward = self.last_reward
            
            state = batch.si[k]
            action = np.argmax(batch.a_r[k][:-1])
            reward = batch.a_r[k][-1]
            self.episode_reward += reward
            pixel_change = batch.pc[k]
            #logger.debug("k = {} of {} -- terminal = {}".format(k,len(batch.si), batch.terminal))
            if k == len(batch.si)-1 and batch.terminal:
                terminal = True
            else:
                terminal = False
            frame = ExperienceFrame(state, reward, action, terminal, pixel_change,
                            last_action, last_reward)
            self.experience.add_frame(frame)
            self.last_action = action
            self.last_reward = reward
            
        if terminal:
            total_ep_reward = self.episode_reward
            self.episode_reward = 0
            return total_ep_reward
        else:
            return None
            
    
    def process(self, sess, global_t, summary_writer, summary_op, score_input):
        # Copy weights from shared to local
        sess.run( self.sync )
        # get batch from process_rollout
        rollout = self.pull_batch_from_queue()
        batch = process_rollout(rollout, gamma=0.99, lambda_=1.0)
        self.local_t += len(batch.si)
        if self.local_t % LOG_INTERVAL == 0:
            logger.info("localtime={}".format(self.local_t))
            logger.info("action={}".format(batch.a[-1,:]))
            logger.info(" V={}".format(batch.r[-1]))
        cur_learning_rate = self._anneal_learning_rate(global_t)
        if self.local_t % PERFORMANCE_LOG_INTERVAL == 0:
            self._print_log(global_t)

        feed_dict = {
            self.local_network.base_input: batch.si,
            self.local_network.base_last_action_reward_input: batch.a_r,
            self.local_network.base_a: batch.a,
            self.local_network.base_adv: batch.adv,
            self.local_network.base_r: batch.r,
            self.local_network.base_initial_lstm_state: batch.features,
            # [common]
            self.learning_rate_input: cur_learning_rate
        }
        
        # Calculate gradients and copy them to global netowrk.
        sess.run( self.apply_gradients, feed_dict=feed_dict )
        
        # add batch to experience replay
        total_ep_reward = self._add_batch_to_exp(batch)
        if total_ep_reward is not None:
            summary_str = sess.run(summary_op, feed_dict={score_input: total_ep_reward})
            summary_writer.add_summary(summary_str, global_t)
            summary_writer.flush()
        
        # Return advanced local step size
        #@TODO check what we are doing with the timekeeping
        diff_local_t = self.local_t - global_t
        return diff_local_t
        

