import tensorflow.compat.v1 as tf
import numpy as np
import gym
from stable_baselines.common.vec_env import SubprocVecEnv, DummyVecEnv
from gym.wrappers.filter_observation import FilterObservation
from gym.wrappers import FlattenObservation

import os
import sys
import shutil
from collections import OrderedDict
import pdb
from matplotlib import pyplot as plt
from tqdm import tqdm

from config import config
from policy import Policy
from value import Value
from discriminator import Discriminator
from posterior import Posterior
from buffer import Buffer
import utils as U
from pdb import set_trace as db
import gym_crisp
import joblib


crispPath = os.path.join(os.path.abspath('../'), 'crisp/')
crisp_server_Path = os.path.join(os.path.abspath('../'), 'crisp_game_server/')
sys.path.insert(1, crispPath)
sys.path.insert(1, crisp_server_Path)

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

dir_name = os.path.dirname(__file__)

np.set_printoptions(precision=4)

# print(config)


def get_env(scaler):
    if config.n_cpu == 1:
        my_env = FlattenObservation(
            FilterObservation(
                gym.make(config.env_name,
                         study_name=config.study_name,
                         start_cycle=config.start_cycle,
                         scaler=scaler),
                filter_keys=config.obs_filter_keys))
        # my_env = DummyVecEnv([lambda: FilterObservation(gym.make(config.env_name,
        #                                                       study_name=config.study_name,
        #                                                       start_cycle=config.start_cycle),
        #                                              filter_keys=config.obs_filter_keys)])
    else:
        my_env = SubprocVecEnv([lambda: FlattenObservation(
            FilterObservation(
                gym.make(config.env_name,
                         study_name=config.study_name,
                         start_cycle=config.start_cycle,
                         scaler=scaler),
                filter_keys=config.obs_filter_keys))
                                for _ in range(config.n_cpu)])
    return my_env


def load_expert_traj():

    traj_np = np.load('expert/traj/{}_{}_state.npy'.format(config.env_name, config.condition), allow_pickle=True)
    theta_np = np.load('expert/traj/{}_{}_action.npy'.format(config.env_name, config.condition), allow_pickle=True)
    scaler = joblib.load('expert/traj/{}_{}_scaler.joblib'.format(config.env_name, config.condition))

    print()
    print('expert trajectories shape: ', traj_np.shape)
    print('expert thetas shape: ', theta_np.shape)
    print()
    
    return traj_np, theta_np, scaler


def code_reward_helper(stu_traj_code, r, code_1, code_2, code_3):

    code_1_idx = np.where(stu_traj_code == 0)[0]
    if len(code_1_idx) > 0:
        code_1.extend(r[code_1_idx])

    code_2_idx = np.where(stu_traj_code == 1)[0]
    if len(code_2_idx) > 0:
        code_2.extend(r[code_2_idx])

    code_3_idx = np.where(stu_traj_code == 2)[0]
    if len(code_3_idx) > 0:
        code_3.extend(r[code_3_idx])


def stat_collect(*args):
    for i in range(len(args) // 2):
        args[2 * i].append(args[2 * i + 1])


def print_and_clear(ds):
    for k, v in ds.items():
        print('{}: {:.0f}'.format(str(k), np.mean(v)))
        v.clear()
    print()


def print_and_clear_2(*ds):
    for d in ds:
        for k, v in d.items():
            print('{}: {:.4f}'.format(k, np.mean(v)))
            v.clear()
        print()


def set_target_net_update():
    policy_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='Policy_stu/')
    old_policy_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='Policy_old/')
    old_policy.set_update_op(policy_vars, old_policy_vars)
    old_policy.run_update_op()

    value_net_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='Value_stu/')
    old_value_net_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='Value_old/')
    old_value_net.set_update_op(value_net_vars, old_value_net_vars)
    old_value_net.run_update_op()


def gae(stu_traj_state, stu_traj_action, stu_traj_code):
    dis_score = discriminator.get_stu_score(stu_traj_state, stu_traj_action)
    log_p = posterior.get_log_posterior(stu_traj_state, stu_traj_action, stu_traj_code)
    r = config.dis_coef * dis_score + config.post_coef * log_p

    value = value_net.get_value(stu_traj_state, stu_traj_code)

    value_spvs_reversed = []
    adv_reversed = []

    next_value = None
    for t in reversed(range(config.max_traj_len)):
        curr_r = r[:, t]
        curr_value = value[:, t]
        
        if t == config.max_traj_len - 1:
            value_spvs = curr_r
            adv = curr_r - curr_value
        else:
            td = curr_r + config.gamma * next_value - curr_value
            value_spvs = curr_r + config.gamma * (config.lam * value_spvs_reversed[-1] + (1 - config.lam) * next_value)
            adv = td + config.gamma * config.lam * adv_reversed[-1]

        value_spvs_reversed.append(value_spvs)
        adv_reversed.append(adv)

        next_value = curr_value

    value_spvs = np.array(list(reversed(value_spvs_reversed))).T
    adv = np.array(list(reversed(adv_reversed))).T

    dis_score_mean = np.mean(dis_score)
    log_p_mean = np.mean(log_p)

    return value_spvs, adv, dis_score_mean, log_p_mean


def main():
    traj_np, theta_np, scaler = load_expert_traj()

    global env, sess, policy, old_policy, value_net, old_value_net, \
        posterior, discriminator, buffer, saver, ckpt_name

    env = get_env(scaler)
    sess = U.get_tf_session()
    policy = Policy(config, sess, 'stu')
    old_policy = Policy(config, sess, 'old')
    value_net = Value(config, sess, 'stu')
    old_value_net = Value(config, sess, 'old')
    posterior = Posterior(config, sess)
    discriminator = Discriminator(config, sess)
    buffer = Buffer(config, traj_np, theta_np, old_policy, env)
    saver = tf.train.Saver()
    ckpt_name = os.path.splitext(os.path.basename(config.save_path))[0]

    if os.path.exists(config.load_path + '.index'):
        saver.restore(sess, config.load_path)
        print('\nloaded from load_path \n')
    else:
        print('\nload_path does not exist \n')
        init = tf.global_variables_initializer()
        sess.run(init)

    set_target_net_update()

    dis_losses = []
    dis_expert_scores = []
    dis_stu_scores = []
    post_losses = []
    post_values = []
    dis_scores = []
    log_ps = []
    stu_values = []
    stu_advs = []
    policy_losses = []
    policy_rewards = []
    entropies = []
    action_log_prob_olds = []
    policy_clipped_freqs = []
    value_losses = []
    old_values = []
    values = []
    value_clipped_freqs = []

    dis_stat = OrderedDict([
        ('dis loss', dis_losses),
        ('dis export score', dis_expert_scores),
        ('dis student score', dis_stu_scores)
    ])

    post_stat = OrderedDict([
        ('posterior loss', post_losses),
        ('posterior', post_values)
    ])

    gae_stat = OrderedDict([
        ('dis score in GAE', dis_scores),
        ('log p in GAE', log_ps),
        ('value from GAE', stu_values),
        ('adv from GAE', stu_advs),
    ])

    policy_stat = OrderedDict([
        ('policy loss', policy_losses),
        ('policy reward', policy_rewards),
        ('entropy', entropies),
        ('policy clipped freq', policy_clipped_freqs),
        ('action log p old', action_log_prob_olds),
    ])

    value_stat = OrderedDict([
        ('value loss', value_losses),
        ('value', values),
        ('value clipped freq', value_clipped_freqs),
        ('value old', old_values)
    ])

    code_1 = []
    # code_1_s_r = []
    # code_1_b_r = []
    code_2 = []
    # code_2_s_r = []
    # code_2_b_r = []
    code_3 = []
    # code_3_s_r = []
    # code_3_b_r = []

    code_rewards = OrderedDict([
        ('code_1', code_1),
        ('code_2', code_2),
        ('code_3', code_3)
    ])

    # code_1_reward = OrderedDict([
    #     ('1_f_r', code_1),
    #     ('1_s_r', code_1_s_r),
    #     ('1_b_r', code_1_b_r)
    # ])
    # code_2_reward = OrderedDict([
    #     ('2_f_r', code_2),
    #     ('2_s_r', code_2_s_r),
    #     ('2_b_r', code_2_b_r)
    # ])
    # code_3_reward = OrderedDict([
    #     ('3_f_r', code_3),
    #     ('3_s_r', code_3_s_r),
    #     ('3_b_r', code_3_b_r)
    # ])

    if config.mode == 'test':
        for i in tqdm(range(config.test_itr)):
            stu_traj_state, stu_traj_action, stu_traj_code, reward = buffer.sample_stu_traj()
            code_reward_helper(stu_traj_code, reward, code_1, code_2, code_3)
        
        print_and_clear(code_rewards)
    
    elif config.mode == 'render':
        # rollout trajectory

        order_data = {}
        allocation_data = {}
        rewards_data = {}

        for _ in tqdm(range(300)):
            stu_traj_code_np = np.random.randint(config.num_code, size=config.batch_size_traj)
            rewards = []
            orders = []
            allocations = []

            init_h_state = None
            init_state = env.reset()
            curr_h_state = init_h_state
            curr_state = init_state[:config.state_dim]

            for _ in range(config.max_traj_len):
                action_sampled, curr_h_state = policy.sample_action(curr_state, stu_traj_code_np, curr_h_state)

                next_state, reward, done, info = env.step(action_sampled[0])

                # env.render()
                curr_state = next_state[:config.state_dim]

                rewards.append(reward)
                orders.append(info.get('order', None))
                allocations.append(info.get('allocation', None))

            if stu_traj_code_np[0] not in order_data:
                order_data.update({stu_traj_code_np[0]: np.array([orders])})
                allocation_data.update({stu_traj_code_np[0]: np.array([allocations])})
                rewards_data.update({stu_traj_code_np[0]: np.array([rewards])})
            else:
                order_data[stu_traj_code_np[0]] = np.append(
                    order_data[stu_traj_code_np[0]], [orders], axis=0)
                allocation_data[stu_traj_code_np[0]] = np.append(
                    allocation_data[stu_traj_code_np[0]], [allocations], axis=0)
                rewards_data[stu_traj_code_np[0]] = np.append(
                    rewards_data[stu_traj_code_np[0]], [rewards], axis=0)

            # print('code: ', stu_traj_code_np[0])
            # print('reward: ', np.sum(rewards))
            # print()

        for code in order_data.keys():
            np.savetxt(f'render/order_data_{config.condition}_code{code}.csv',
                       order_data[code], delimiter=',')
            np.savetxt(f'render/allocation_data_{config.condition}_code{code}.csv',
                       allocation_data[code], delimiter=',')
            np.savetxt(f'render/rewards_data_{config.condition}_code{code}.csv',
                       rewards_data[code], delimiter=',')


    else:
        for i in tqdm(range(1, config.itr + 1)):

            for j in range(1, config.inner_itr_1 + 1):
                stu_traj_state, stu_traj_action, stu_traj_code, reward = buffer.sample_stu_traj()
                code_reward_helper(stu_traj_code, reward, code_1, code_2, code_3)

                expert_traj_state, expert_traj_action = buffer.sample_expert_traj()

                dis_loss, dis_expert_score, dis_stu_score = discriminator.train(expert_traj_state, expert_traj_action,
                                                                                stu_traj_state, stu_traj_action)
                post_loss, post_value = posterior.train(stu_traj_state, stu_traj_action, stu_traj_code)

                if j == config.inner_itr_1:
                    stat_collect(dis_losses, dis_loss, dis_expert_scores, dis_expert_score, dis_stu_scores, dis_stu_score)
                    stat_collect(post_losses, post_loss, post_values, post_value)
            
            for k in range(1, config.inner_itr_2 + 1):
                stu_traj_state, stu_traj_action, stu_traj_code, reward = buffer.sample_stu_traj()
                code_reward_helper(stu_traj_code, reward, code_1, code_2, code_3)

                stu_traj_value_spvs, stu_traj_adv, dis_score, log_p = gae(stu_traj_state, stu_traj_action, stu_traj_code)
                action_log_prob_old = old_policy.get_action_log_prob(stu_traj_state, stu_traj_code, stu_traj_action)
                stu_traj_value_old = old_value_net.get_value(stu_traj_state, stu_traj_code)

                policy_loss, policy_reward, entropy, policy_clipped_freq = policy.train(stu_traj_state, stu_traj_code, stu_traj_action, 
                    stu_traj_adv, action_log_prob_old)

                value_loss, value, value_clipped_freq = value_net.train(stu_traj_state, stu_traj_code, stu_traj_value_spvs, stu_traj_value_old)
                
                if k == config.inner_itr_2:
                    stat_collect(
                        dis_scores, dis_score, 
                        log_ps, log_p, 
                        stu_values, stu_traj_value_spvs, 
                        stu_advs, stu_traj_adv, 
                        policy_losses, policy_loss,
                        policy_rewards, policy_reward, 
                        entropies, entropy,
                        policy_clipped_freqs, policy_clipped_freq, 
                        action_log_prob_olds, action_log_prob_old, 
                        value_losses, value_loss,
                        values, value,
                        value_clipped_freqs, value_clipped_freq,
                        old_values, stu_traj_value_old
                    )

                if k % config.update_period == 0:
                    old_policy.run_update_op()
                    old_value_net.run_update_op()

            if i % config.print_itr == 0:
                print('[t{}]'.format(i))
                print_and_clear_2(dis_stat, post_stat, gae_stat, policy_stat, value_stat, code_rewards)

            if i % config.save_itr == 0:
                saver.save(sess, config.save_path)
                print('ckpt saved\n')

    env.close()


if __name__ == "__main__":
    main()
