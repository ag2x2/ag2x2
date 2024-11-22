from datetime import datetime
import os
import time
import cv2
from gym.spaces import Space
import gym
from gym.wrappers.monitoring.video_recorder import VideoRecorder

import numpy as np
import statistics
import pickle
from collections import deque

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter
import wandb

from algos.rl.ppo import RolloutStorage
from algos.rl.ppo import ActorCritic

import copy

class PPO:
    def __init__(self,
                 vec_env,
                 cfg_train,
                 device='cpu',
                 sampler='sequential',
                 log_dir='run',
                 is_testing=False,
                 print_log=True,
                 apply_reset=False,
                 asymmetric=False
                 ):

        if not isinstance(vec_env.observation_space, Space):
            raise TypeError("vec_env.observation_space must be a gym Space")
        if not isinstance(vec_env.state_space, Space):
            raise TypeError("vec_env.state_space must be a gym Space")
        if not isinstance(vec_env.action_space, Space):
            raise TypeError("vec_env.action_space must be a gym Space")
        self.observation_space = vec_env.observation_space
        self.action_space = vec_env.action_space
        self.state_space = vec_env.state_space
        self.cfg_train = copy.deepcopy(cfg_train)
        learn_cfg = self.cfg_train["learn"]
        self.device = device
        self.asymmetric = asymmetric
        self.desired_kl = learn_cfg.get("desired_kl", None)
        self.schedule = learn_cfg.get("schedule", "fixed")
        self.step_size = learn_cfg["optim_stepsize"]
        self.init_noise_std = learn_cfg.get("init_noise_std", 0.3)
        self.model_cfg = self.cfg_train["policy"]
        self.num_transitions_per_env=learn_cfg["nsteps"]
        self.learning_rate=learn_cfg["optim_stepsize"]

        self.save_traj = self.cfg_train.get("save_traj", False)

        # PPO components
        self.vec_env = vec_env
        print(self.observation_space.shape)
        self.actor_critic = ActorCritic(self.observation_space.shape, self.state_space.shape, self.action_space.shape,
                                               self.init_noise_std, self.model_cfg, asymmetric=asymmetric)
        self.actor_critic.to(self.device)
        self.storage = RolloutStorage(self.vec_env.num_envs, self.num_transitions_per_env, self.observation_space.shape,
                                      self.state_space.shape, self.action_space.shape, self.device, sampler)
        self.optimizer = optim.Adam(self.actor_critic.parameters(), lr=self.learning_rate)
    
        # PPO parameters
        self.clip_param = learn_cfg["cliprange"]
        self.num_learning_epochs = learn_cfg["noptepochs"]
        self.num_mini_batches = learn_cfg["nminibatches"]
        self.num_transitions_per_env = self.num_transitions_per_env
        self.value_loss_coef = learn_cfg.get("value_loss_coef", 2.0)
        self.entropy_coef = learn_cfg["ent_coef"]
        self.gamma = learn_cfg["gamma"]
        self.lam = learn_cfg["lam"]
        self.max_grad_norm = learn_cfg.get("max_grad_norm", 2.0)
        self.use_clipped_value_loss = learn_cfg.get("use_clipped_value_loss", False)
        self.total_loss_coef = learn_cfg.get("total_loss_coef", 1.0)

        # Log
        self.log_dir = log_dir
        self.print_log = print_log
        self.writer = SummaryWriter(log_dir=self.log_dir, flush_secs=10)
        self.tot_timesteps = 0
        self.tot_time = 0
        self.is_testing = is_testing
        self.current_learning_iteration = 0

        ## wandb logger
        self.apply_reset = apply_reset

    def test(self, path):
        self.actor_critic.load_state_dict(torch.load(path))
        self.actor_critic.eval()
        self.model_basedir = os.path.dirname(path)
        self.model_name = os.path.basename(path)

    def load(self, path):
        self.actor_critic.load_state_dict(torch.load(path))
        self.current_learning_iteration = int(path.split("_")[-1].split(".")[0])
        self.actor_critic.train()

    def save(self, path):
        torch.save(self.actor_critic.state_dict(), path)

    def run(self, num_learning_iterations, log_interval=1, ckpt=None):
        current_obs = self.vec_env.reset()
        current_states = self.vec_env.get_state()

        if self.is_testing:
            if self.save_traj:
                assert current_obs.shape[0] == 1, "Saving trajectory only works with one environment"
                trajectory = []
                scores = []
                visual_reward = []
                self.actor_critic.load_state_dict(torch.load(ckpt))

                self.actor_critic.eval()
                while True:
                    with torch.no_grad():
                        actions = self.actor_critic.act_inference(current_obs)
                        next_obs, rews, dones, infos = self.vec_env.step(actions)
                        current_obs.copy_(next_obs)
                        trajectory.append(self.vec_env.task.get_states())
                        scores.append(infos['success_scores'].cpu().item())
                        visual_reward.append(rews.cpu().numpy())
                        
                    if self.vec_env.task.reset_buf.sum() > 0.:
                        break
                
                # save trajectory with pickle
                absres_path = os.path.join(self.model_basedir, f'absres_{os.path.splitext(self.model_name)[0][6:]}.pkl')
                absres = {
                    'trajectory': trajectory,
                    'scores': scores,
                    'visual_reward': visual_reward
                }
                pickle.dump(absres, open(absres_path, 'wb'))
                print(f'Save trajectory to {absres_path}')
            else:
                while True:
                    with torch.no_grad():
                        if self.apply_reset:
                            current_obs = self.vec_env.reset()
                        # Compute the action
                        actions = self.actor_critic.act_inference(current_obs)
                        # Step the vec_environment
                        next_obs, rews, dones, infos = self.vec_env.step(actions)
                        current_obs.copy_(next_obs)
        else:
            rewbuffer = deque(maxlen=100)  #! length of rewbuffer matters?
            lenbuffer = deque(maxlen=100)
            cur_reward_sum = torch.zeros(self.vec_env.num_envs, dtype=torch.float, device=self.device)
            cur_episode_length = torch.zeros(self.vec_env.num_envs, dtype=torch.float, device=self.device)

            reward_sum = []
            episode_length = []
            best_mean_reward = -np.inf
            for it in range(self.current_learning_iteration, num_learning_iterations):
                start = time.time()
                ep_infos = []

                # Rollout
                for _ in range(self.num_transitions_per_env):
                    if self.apply_reset:
                        current_obs = self.vec_env.reset()
                        current_states = self.vec_env.get_state()
                    # Compute the action
                    actions, actions_log_prob, values, mu, sigma = self.actor_critic.act(current_obs, current_states)
                    # Step the vec_environment
                    next_obs, rews, dones, infos = self.vec_env.step(actions)
                    next_states = self.vec_env.get_state()
                    # Record the transition
                    self.storage.add_transitions(current_obs, current_states, actions, rews, dones, values, actions_log_prob, mu, sigma)
                    current_obs.copy_(next_obs)
                    current_states.copy_(next_states)
                    # Book keeping
                    ep_infos.append(infos)

                    if self.print_log:
                        cur_reward_sum[:] += rews
                        cur_episode_length[:] += 1

                        new_ids = (dones > 0).nonzero(as_tuple=False)
                        reward_sum.extend(cur_reward_sum[new_ids][:, 0].cpu().numpy().tolist())
                        episode_length.extend(cur_episode_length[new_ids][:, 0].cpu().numpy().tolist())
                        cur_reward_sum[new_ids] = 0
                        cur_episode_length[new_ids] = 0

                if self.print_log:
                    # reward_sum = [x[0] for x in reward_sum]
                    # episode_length = [x[0] for x in episode_length]
                    rewbuffer.extend(reward_sum)
                    lenbuffer.extend(episode_length)

                _, _, last_values, _, _ = self.actor_critic.act(current_obs, current_states)
                stop = time.time()
                collection_time = stop - start

                mean_trajectory_length, mean_reward = self.storage.get_statistics()

                # only save ckpt when mean reward is better
                if mean_reward > best_mean_reward:
                    best_mean_reward = mean_reward
                    self.save(os.path.join(self.log_dir, 'model_best.pt'.format(it)))
                    # self.save(os.path.join(self.log_dir, 'model_{}.pt'.format(it)))
                if it % (log_interval) == 0:
                    self.save(os.path.join(self.log_dir, 'model_{}.pt'.format(it)))

                # Learning step
                start = stop
                self.storage.compute_returns(last_values, self.gamma, self.lam)
                mean_value_loss, mean_surrogate_loss, mean_gradient_norm, mean_gradient_norm_clip, mean_learning_rate = self.update()
                self.storage.clear()
                stop = time.time()
                learn_time = stop - start
                if self.print_log:
                    self.log(locals())
                
                ep_infos.clear()
            self.save(os.path.join(self.log_dir, 'model_{}.pt'.format(num_learning_iterations)))

    def log(self, locs, width=80, pad=35):
        self.tot_timesteps += self.num_transitions_per_env * self.vec_env.num_envs
        self.tot_time += locs['collection_time'] + locs['learn_time']
        iteration_time = locs['collection_time'] + locs['learn_time']

        ep_string = f''
        if locs['ep_infos']:
            for key in locs['ep_infos'][0]:
                infotensor = torch.tensor([], device=self.device)
                for ep_info in locs['ep_infos']:
                    infotensor = torch.cat((infotensor, ep_info[key].to(self.device)))
                value = torch.mean(infotensor)
                self.writer.add_scalar('Episode/' + key, value, locs['it'])
                if wandb.run is not None:
                    wandb.log({'Episode/' + key: value}, step=locs['it'])
                    # wandb.log({'Episode/' + key: value}, )
                ep_string += f"""{f'Mean episode {key}:':>{pad}} {value:.4f}\n"""
        mean_std = self.actor_critic.log_std.exp().mean()

        self.writer.add_scalar('Loss/value_function', locs['mean_value_loss'], locs['it'])
        self.writer.add_scalar('Loss/surrogate', locs['mean_surrogate_loss'], locs['it'])
        self.writer.add_scalar('Policy/mean_noise_std', mean_std.item(), locs['it'])
        if wandb.run is not None:
            wandb.log({
                'Loss/value_function': locs['mean_value_loss'],
                'Loss/surrogate': locs['mean_surrogate_loss'],
                'Policy/mean_noise_std': mean_std.item(),
                
                'Optim/mean_grad_norm': locs['mean_gradient_norm'],
                'Optim/mean_grad_norm_clip': locs['mean_gradient_norm_clip'],
                'Optim/mean_learning_rate': locs['mean_learning_rate'],
            }, step=locs['it'])
        
        if len(locs['rewbuffer']) > 0:
            self.writer.add_scalar('Train/mean_reward', statistics.mean(locs['rewbuffer']), locs['it'])
            self.writer.add_scalar('Train/mean_episode_length', statistics.mean(locs['lenbuffer']), locs['it'])
            self.writer.add_scalar('Train/mean_reward/time', statistics.mean(locs['rewbuffer']), self.tot_time)
            self.writer.add_scalar('Train/mean_episode_length/time', statistics.mean(locs['lenbuffer']), self.tot_time)
            if wandb.run is not None:
                wandb.log({
                    'Train/mean_reward': statistics.mean(locs['rewbuffer']),
                    'Train/mean_episode_length': statistics.mean(locs['lenbuffer']),
                }, step=locs['it'])
                # wandb.log({
                #     'Train/mean_reward/time': statistics.mean(locs['rewbuffer']),
                #     'Train/mean_episode_length/time': statistics.mean(locs['lenbuffer']),
                # }, step=int(self.tot_time))

        self.writer.add_scalar('Train2/mean_reward/step', locs['mean_reward'], locs['it'])
        self.writer.add_scalar('Train2/mean_episode_length/episode', locs['mean_trajectory_length'], locs['it'])
        # if wandb.run is not None:
        #     wandb.log({
        #         'Train2/mean_reward/step': locs['mean_reward'],
        #         'Train2/mean_episode_length/episode': locs['mean_trajectory_length'],
        #     })
        
        fps = int(self.num_transitions_per_env * self.vec_env.num_envs / (locs['collection_time'] + locs['learn_time']))

        str = f" \033[1m Learning iteration {locs['it']}/{locs['num_learning_iterations']} \033[0m "

        if len(locs['rewbuffer']) > 0:
            log_string = (f"""{'#' * width}\n"""
                          f"""{str.center(width, ' ')}\n\n"""
                          f"""{'Computation:':>{pad}} {fps:.0f} steps/s (collection: {locs[
                              'collection_time']:.3f}s, learning {locs['learn_time']:.3f}s)\n"""
                          f"""{'Value function loss:':>{pad}} {locs['mean_value_loss']:.4f}\n"""
                          f"""{'Surrogate loss:':>{pad}} {locs['mean_surrogate_loss']:.4f}\n"""
                          f"""{'Mean action noise std:':>{pad}} {mean_std.item():.2f}\n"""
                          f"""{'Mean reward:':>{pad}} {statistics.mean(locs['rewbuffer']):.2f}\n"""
                          f"""{'Mean episode length:':>{pad}} {statistics.mean(locs['lenbuffer']):.2f}\n"""
                          f"""{'Mean reward/step:':>{pad}} {locs['mean_reward']:.2f}\n"""
                          f"""{'Mean episode length/episode:':>{pad}} {locs['mean_trajectory_length']:.2f}\n""")
        else:
            log_string = (f"""{'#' * width}\n"""
                          f"""{str.center(width, ' ')}\n\n"""
                          f"""{'Computation:':>{pad}} {fps:.0f} steps/s (collection: {locs[
                            'collection_time']:.3f}s, learning {locs['learn_time']:.3f}s)\n"""
                          f"""{'Value function loss:':>{pad}} {locs['mean_value_loss']:.4f}\n"""
                          f"""{'Surrogate loss:':>{pad}} {locs['mean_surrogate_loss']:.4f}\n"""
                          f"""{'Mean action noise std:':>{pad}} {mean_std.item():.2f}\n"""
                          f"""{'Mean reward/step:':>{pad}} {locs['mean_reward']:.2f}\n"""
                          f"""{'Mean episode length/episode:':>{pad}} {locs['mean_trajectory_length']:.2f}\n""")

        log_string += ep_string
        log_string += (f"""{'-' * width}\n"""
                       f"""{'Total timesteps:':>{pad}} {self.tot_timesteps}\n"""
                       f"""{'Iteration time:':>{pad}} {iteration_time:.2f}s\n"""
                       f"""{'Total time:':>{pad}} {self.tot_time:.2f}s\n"""
                       f"""{'ETA:':>{pad}} {self.tot_time / (locs['it'] + 1) * (
                               locs['num_learning_iterations'] - locs['it']):.1f}s\n""")
        print(log_string)

    def update(self):
        mean_value_loss = 0
        mean_surrogate_loss = 0
        mean_gradient_norm = 0
        mean_gradient_norm_clip = 0
        mean_learning_rate = 0

        batch = self.storage.mini_batch_generator(self.num_mini_batches)
        for epoch in range(self.num_learning_epochs):
            # for obs_batch, actions_batch, target_values_batch, advantages_batch, returns_batch, old_actions_log_prob_batch \
            #        in self.storage.mini_batch_generator(self.num_mini_batches):

            for indices in batch:
                obs_batch = self.storage.observations.view(-1, *self.storage.observations.size()[2:])[indices]
                if self.asymmetric:
                    states_batch = self.storage.states.view(-1, *self.storage.states.size()[2:])[indices]
                else:
                    states_batch = None
                actions_batch = self.storage.actions.view(-1, self.storage.actions.size(-1))[indices]
                target_values_batch = self.storage.values.view(-1, 1)[indices]
                returns_batch = self.storage.returns.view(-1, 1)[indices]
                old_actions_log_prob_batch = self.storage.actions_log_prob.view(-1, 1)[indices]
                advantages_batch = self.storage.advantages.view(-1, 1)[indices]
                old_mu_batch = self.storage.mu.view(-1, self.storage.actions.size(-1))[indices]
                old_sigma_batch = self.storage.sigma.view(-1, self.storage.actions.size(-1))[indices]

                actions_log_prob_batch, entropy_batch, value_batch, mu_batch, sigma_batch = self.actor_critic.evaluate(obs_batch,
                                                                                                                       states_batch,
                                                                                                                       actions_batch)

                # KL
                if self.desired_kl != None and self.schedule == 'adaptive':

                    kl = torch.sum(
                        sigma_batch - old_sigma_batch + (torch.square(old_sigma_batch.exp()) + torch.square(old_mu_batch - mu_batch)) / (2.0 * torch.square(sigma_batch.exp())) - 0.5, axis=-1)
                    kl_mean = torch.mean(kl)

                    if kl_mean > self.desired_kl * 2.0:
                        self.step_size = max(1e-5, self.step_size / 1.5)
                    elif kl_mean < self.desired_kl / 2.0 and kl_mean > 0.0:
                        self.step_size = min(1e-2, self.step_size * 1.5)

                    for param_group in self.optimizer.param_groups:
                        param_group['lr'] = self.step_size

                # Surrogate loss
                ratio = torch.exp(actions_log_prob_batch - torch.squeeze(old_actions_log_prob_batch))
                surrogate = -torch.squeeze(advantages_batch) * ratio
                surrogate_clipped = -torch.squeeze(advantages_batch) * torch.clamp(ratio, 1.0 - self.clip_param,
                                                                                   1.0 + self.clip_param)
                surrogate_loss = torch.max(surrogate, surrogate_clipped).mean()

                # Value function loss
                if self.use_clipped_value_loss:
                    value_clipped = target_values_batch + (value_batch - target_values_batch).clamp(-self.clip_param,
                                                                                                    self.clip_param)
                    value_losses = (value_batch - returns_batch).pow(2)
                    value_losses_clipped = (value_clipped - returns_batch).pow(2)
                    value_loss = torch.max(value_losses, value_losses_clipped).mean()
                else:
                    value_loss = (returns_batch - value_batch).pow(2).mean()

                loss = surrogate_loss + self.value_loss_coef * value_loss - self.entropy_coef * entropy_batch.mean()
                # scaling loss
                loss = self.total_loss_coef * loss

                # Gradient step
                self.optimizer.zero_grad()
                loss.backward()

                # Compute the gradient norm before clip
                grad_norm = 0
                for p in self.actor_critic.parameters():
                    param_norm = p.grad.data.norm(2)
                    grad_norm += param_norm.item() ** 2
                grad_norm = grad_norm ** (1. / 2)
                mean_gradient_norm += grad_norm

                nn.utils.clip_grad_norm_(self.actor_critic.parameters(), self.max_grad_norm)
                
                # Compute the gradient norm after clip
                grad_norm_clip = 0
                for p in self.actor_critic.parameters():
                    param_norm = p.grad.data.norm(2)
                    grad_norm_clip += param_norm.item() ** 2
                grad_norm_clip = grad_norm_clip ** (1. / 2)
                mean_gradient_norm_clip += grad_norm_clip

                # Record the learning rate
                mean_learning_rate += self.optimizer.param_groups[0]['lr']
                
                self.optimizer.step()

                mean_value_loss += value_loss.item()
                mean_surrogate_loss += surrogate_loss.item()

        num_updates = self.num_learning_epochs * self.num_mini_batches
        mean_value_loss /= num_updates
        mean_surrogate_loss /= num_updates
        mean_gradient_norm /= num_updates
        mean_gradient_norm_clip /= num_updates
        mean_learning_rate /= num_updates

        return mean_value_loss, mean_surrogate_loss, mean_gradient_norm, mean_gradient_norm_clip, mean_learning_rate
