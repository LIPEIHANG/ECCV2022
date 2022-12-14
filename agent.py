# R2R-EnvDrop, 2019, haotan@cs.unc.edu
# Modified in Recurrent VLN-BERT, 2020, by Yicong.Hong@anu.edu.au

import json
import os
import sys
import numpy as np
import random
import math
import time

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F

from env import R2RBatch
import utils
from utils import padding_idx, print_progress
import model_PREVALENT
import param
from param import args
from collections import defaultdict
from shared_optim import ensure_shared_grads


class BaseAgent(object):
    ''' Base class for an R2R agent to generate and save trajectories. '''

    def __init__(self, env, results_path):
        self.env = env
        self.results_path = results_path
        random.seed(1)
        self.results = {}
        self.losses = [] # For learning agents

    def write_results(self):
        output = [{'instr_id':k, 'trajectory': v} for k,v in self.results.items()]
        with open(self.results_path, 'w') as f:
            json.dump(output, f)

    def get_results(self):
        output = [{'instr_id': k, 'trajectory': v} for k, v in self.results.items()]
        return output

    def rollout(self, **args):
        ''' Return a list of dicts containing instr_id:'xx', path:[(viewpointId, heading_rad, elevation_rad)]  '''
        raise NotImplementedError

    @staticmethod
    def get_agent(name):
        return globals()[name+"Agent"]  # 以字典类型返回当前位置的全部全局变量

    def test(self, iters=None, **kwargs):
        self.env.reset_epoch(shuffle=(iters is not None))   # If iters is not none, shuffle the env batch
        self.losses = []
        self.results = {}
        # We rely on env showing the entire batch before repeating anything
        looped = False
        self.loss = 0
        if iters is not None:
            # For each time, it will run the first 'iters' iterations. (It was shuffled before)
            for i in range(iters):
                for traj in self.rollout(**kwargs):
                    self.loss = 0
                    self.results[traj['instr_id']] = traj['path']
        else:   # Do a full round
            while True:
                for traj in self.rollout(**kwargs):
                    if traj['instr_id'] in self.results:
                        looped = True
                    else:
                        self.loss = 0
                        self.results[traj['instr_id']] = traj['path']
                if looped:
                    break


class Seq2SeqAgent(BaseAgent):
    ''' An agent based on an LSTM seq2seq model with attention. '''

    # For now, the agent can't pick which forward move to make - just the one in the middle
    env_actions = {
      'left': (0,-1, 0), # left
      'right': (0, 1, 0), # right
      'up': (0, 0, 1), # up
      'down': (0, 0,-1), # down
      'forward': (1, 0, 0), # forward
      '<end>': (0, 0, 0), # <end>
      '<start>': (0, 0, 0), # <start>
      '<ignore>': (0, 0, 0)  # <ignore>
    }

    def __init__(self, env, results_path, tok, episode_len=20):
        super(Seq2SeqAgent, self).__init__(env, results_path)
        self.tok = tok
        self.episode_len = episode_len
        self.feature_size = args.feature_size
        self.batch_size = args.batchSize

        # Models
        if args.vlnbert == 'prevalent':
            self.vln_bert = model_PREVALENT.VLNBERT(feature_size=self.feature_size + args.angle_feat_size).cuda()
            self.critic = model_PREVALENT.Critic().cuda()
        self.models = (self.vln_bert, self.critic)

        # Optimizers
        self.vln_bert_optimizer = args.optimizer(self.vln_bert.parameters(), lr=args.lr)
        self.critic_optimizer = args.optimizer(self.critic.parameters(), lr=args.lr)
        self.optimizers = (self.vln_bert_optimizer, self.critic_optimizer)

        # Evaluations
        self.losses = []
        self.criterion = nn.CrossEntropyLoss(ignore_index=args.ignoreid, size_average=False)
        self.ndtw_criterion = utils.ndtw_initialize()
        self.disloss = nn.KLDivLoss()

        # Logs
        sys.stdout.flush()
        self.logs = defaultdict(list)

    def _sort_batch(self, obs):
        seq_tensor = np.array([ob['instr_encoding'] for ob in obs])
        seq_lengths = np.argmax(seq_tensor == padding_idx, axis=1)
        seq_lengths[seq_lengths == 0] = seq_tensor.shape[1]

        seq_tensor = torch.from_numpy(seq_tensor)
        seq_lengths = torch.from_numpy(seq_lengths)

        # Sort sequences by lengths
        seq_lengths, perm_idx = seq_lengths.sort(0, True)  # True -> descending
        sorted_tensor = seq_tensor[perm_idx]
        mask = (sorted_tensor != padding_idx)

        token_type_ids = torch.zeros_like(mask)

        return Variable(sorted_tensor, requires_grad=False).long().cuda(), \
               mask.long().cuda(), token_type_ids.long().cuda(), \
               list(seq_lengths), list(perm_idx)

    def _feature_variable(self, obs):
        ''' Extract precomputed features into variable. '''
        features = np.empty((len(obs), args.views, self.feature_size + args.angle_feat_size), dtype=np.float32)
        for i, ob in enumerate(obs):
            features[i, :, :] = ob['feature']  # Image feat
        return Variable(torch.from_numpy(features), requires_grad=False).cuda()

    def _candidate_variable(self, obs, t, vis_taj, input_a_t):
        candidate_leng = [len(ob['candidate']) + 1 + t for ob in obs]  # +1 is for the end
        candidate_feat = np.zeros((len(obs), max(candidate_leng), self.feature_size + args.angle_feat_size), dtype=np.float32)
        candidate_feat = torch.from_numpy(candidate_feat).cuda()

        if t > 0:
            candidate_feat[:, :t-1, :] = vis_taj[:, :t-1, :]
            candidate_feat[:, t-1, :self.feature_size] = vis_taj[:, t-1, :self.feature_size]
            candidate_feat[:, t-1, -args.angle_feat_size:] = input_a_t

        for i, ob in enumerate(obs):
            for j, cc in enumerate(ob['candidate']):
                cc_feature = torch.from_numpy(cc['feature']).cuda()
                candidate_feat[i, j+t, :] = cc_feature

        return candidate_feat, candidate_leng

    def get_input_feat(self, obs, t, vis_taj):
        input_a_t = np.zeros((len(obs), args.angle_feat_size), np.float32)
        for i, ob in enumerate(obs):
            input_a_t[i] = utils.angle_feature(ob['heading'], ob['elevation'])
        input_a_t = torch.from_numpy(input_a_t).cuda()
        # f_t = self._feature_variable(obs)      # Pano image features from obs
        candidate_feat, candidate_leng = self._candidate_variable(obs, t, vis_taj, input_a_t)

        return input_a_t, candidate_feat, candidate_leng

    def _teacher_action(self, obs, ended):
        """
        Extract teacher actions into variable.
        :param obs: The observation.
        :param ended: Whether the action seq is ended
        :return:
        """
        a = np.zeros(len(obs), dtype=np.int64)
        for i, ob in enumerate(obs):
            if ended[i]:                                            # Just ignore this index
                a[i] = args.ignoreid
            else:
                for k, candidate in enumerate(ob['candidate']):
                    if candidate['viewpointId'] == ob['teacher']:   # Next view point
                        a[i] = k
                        break
                else:   # Stop here
                    # assert ob['teacher'] == ob['viewpoint']         # The teacher action should be "STAY HERE"
                    a[i] = len(ob['candidate'])
        return torch.from_numpy(a).cuda()

    def make_equiv_action(self, a_t, perm_obs, perm_idx=None, traj=None):
        """
        Interface between Panoramic view and Egocentric view
        It will convert the action panoramic view action a_t to equivalent egocentric view actions for the simulator
        """
        def take_action(i, idx, name):
            if type(name) is int:       # Go to the next view
                #self.env.env.sims[idx].makeAction(name, 0, 0)
                self.env.env.sims[idx].makeAction([name], [0], [0])
            else:                       # Adjust
                #self.env.env.sims[idx].makeAction(*self.env_actions[name])
                self.env.env.sims[idx].makeAction([*self.env_actions[name]])
        if perm_idx is None:
            perm_idx = range(len(perm_obs))

        for i, idx in enumerate(perm_idx):
            action = a_t[i]
            if action != -1:            # -1 is the <stop> action
                select_candidate = perm_obs[i]['candidate'][action]
                src_point = perm_obs[i]['viewIndex']
                trg_point = select_candidate['pointId']
                src_level = (src_point ) // 12  # The point idx started from 0
                trg_level = (trg_point ) // 12
                while src_level < trg_level:    # Tune up
                    take_action(i, idx, 'up')
                    src_level += 1
                while src_level > trg_level:    # Tune down
                    take_action(i, idx, 'down')
                    src_level -= 1
                #getState() -> getState()[0]
                while self.env.env.sims[idx].getState()[0].viewIndex != trg_point:    # Turn right until the target
                    take_action(i, idx, 'right')
                assert select_candidate['viewpointId'] == \
                       self.env.env.sims[idx].getState()[0].navigableLocations[select_candidate['idx']].viewpointId
                take_action(i, idx, select_candidate['idx'])

                state = self.env.env.sims[idx].getState()[0]
                if traj is not None:
                    traj[i]['path'].append((state.location.viewpointId, state.heading, state.elevation))

    def rollout(self, train_ml=None, train_dis_l=None, train_dis_c=None, att_drop_rate=0.5, train_rl=True, reset=True): # 在环境中进行一次探索，然后决定action
        """
        :param train_ml:    The weight to train with maximum likelihood
        :param train_rl:    whether use RL in training
        :param reset:       Reset the environment

        :return:
        """
        if self.feedback == 'teacher' or self.feedback == 'argmax': # 探索的几种模式，在rollout()的后半部分会提到
            train_rl = False

        if reset:  # Reset env 新建重启一个环境
            obs = np.array(self.env.reset())
        else:
            obs = np.array(self.env._get_obs())

        batch_size = len(obs)  # ob=["instruction":{},"viewpointId":{}, "heading":{},"elevation":{}，“distance”:{},...]表示在此时的位置所获得的信息
        dis_loss_l = 0.        # obs=[ob1, ob2, ...obn]

        # Language input
        sentence, language_attention_mask, token_type_ids, \
            seq_lengths, perm_idx = self._sort_batch(obs)   # 根据sentence的长度由长到短进行排序, eg: sentence2,sentence1,sentence0
        perm_obs = obs[perm_idx] # 对obs根据sentence长度进行排序,eg:perm_obs=[ob2,ob1,ob0]

        ''' Language Embedding '''
        language_inputs = {'mode':        'language',
                        'sentence':       sentence,
                        'attention_mask': language_attention_mask, #对句子的attention做mask处理，需要mask掉哪些词,eg:language_attention_mask=[0,1,...,0],1表示被mask掉
                        'lang_mask':      language_attention_mask, #对句子本身做mask处理，并且要做padding处理，把句子长度都补成80
                        'token_type_ids': token_type_ids}

        # sentence embedding
        if args.vlnbert == 'prevalent': #recurrent VLN Bert模型的一种模式
            text_embeds = self.vln_bert(**language_inputs) # 只针对语言进行embedding

        language_attention_mask_drop = torch.rand_like(language_attention_mask.float()) < att_drop_rate #对句子的attention做mask处理，若language_attention_mask对应产生的随机数< att_drop_rate就mask掉.
        language_attention_mask_drop = language_attention_mask_drop.long()
        language_attention_mask_drop = language_attention_mask_drop.mul(language_attention_mask) #mul()表示乘法，最终的language_attention_mask_drop综合考虑了padding和attention
        language_inputs_drop = {'mode':        'language',
                        'sentence':       sentence,
                        'attention_mask': language_attention_mask_drop,
                        'lang_mask':      language_attention_mask_drop,
                        'token_type_ids': token_type_ids}
        if args.vlnbert == 'prevalent':
            text_embeds_drop = self.vln_bert(**language_inputs_drop)

        log_probs1 = F.log_softmax(text_embeds_drop.clone(), 1) #dis_loss_1对应论文第(5)式
        probs2 = F.softmax(text_embeds.clone().detach(), 1)
        dis_loss_l += self.disloss(log_probs1, probs2) # disloss用来求KL散度

        log_probs2 = F.log_softmax(text_embeds.clone(), 1)
        probs1 = F.softmax(text_embeds_drop.clone().detach(), 1)
        dis_loss_l += self.disloss(log_probs2, probs1)

        language_features = text_embeds
        # Record starting point
        traj = [{
            'instr_id': ob['instr_id'],
            'path': [(ob['viewpoint'], ob['heading'], ob['elevation'])],
        } for ob in perm_obs]

        # Init the reward shaping
        last_dist = np.zeros(batch_size, np.float32) # 表示当前agent距离最终目的地的距离，越近奖励越大
        last_ndtw = np.zeros(batch_size, np.float32) # 表示当前路径与ground truth路径的相似度，相似度越大，则奖励越大
        for i, ob in enumerate(perm_obs):   # The init distance from the view point to the target
            last_dist[i] = ob['distance']
            path_act = [vp[0] for vp in traj[i]['path']] # 把viewpointId单独提取出来
            last_ndtw[i] = self.ndtw_criterion[ob['scan']](path_act, ob['gt_path'], metric='ndtw')#计算相似性

        # Initialization the tracking state
        ended = np.array([False] * batch_size)  # Indices match permuation of the model, not env
                                                # 终止时ended变成true，有两种情况会发生终止：1.达到最大探索次数(episode_len)2.预测的action为stop

        # Init the logs
        rewards = []
        hidden_states = []
        policy_log_probs = []
        masks = []
        entropys = []
        ml_loss = 0.
        vis_taj = np.zeros((len(obs), self.episode_len, 2176), np.float32)
        vis_taj = torch.from_numpy(vis_taj).cuda() # 可以类比论文中的memory bank,表示视觉上的历史轨迹

        for t in range(self.episode_len): #episode_len表示最大探索次数

            input_a_t, candidate_feat, candidate_leng = self.get_input_feat(perm_obs,t,vis_taj) #input_a_t表示输入的action，candidate_feat 可以理解为未经过MLP的m_t包含了
            candidate_feat_for_traj = candidate_feat.clone()

            visual_temp_mask = (utils.length2mask(candidate_leng) == 0).long() #对视觉图像进行mask处理
            visual_attention_mask = torch.cat((language_attention_mask, visual_temp_mask), dim=-1)#对视觉图像的attention进行mask处理
                
            ''' Visual BERT '''
            visual_inputs = {'mode':              'visual',
                            'sentence':           language_features,
                            'attention_mask':     visual_attention_mask,
                            'lang_mask':          language_attention_mask,
                            'vis_mask':           visual_temp_mask,
                            'token_type_ids':     token_type_ids,
                            'action_feats':       input_a_t,
                            # 'pano_feats':         f_t,
                            'cand_feats':         candidate_feat,
                            't':                  t,
                            'seq_lengths':        seq_lengths,
                            'att_drop_rate':      att_drop_rate}
            h_t, visn_output, logit, dis_loss_c = self.vln_bert(**visual_inputs)
            hidden_states.append(h_t)

            # Mask outputs where agent can't move forward
            # Here the logit is [b, max_candidate]

            candidate_mask = utils.length2mask([i-t for i in candidate_leng])
            logit.masked_fill_(candidate_mask, -float('inf'))

            # Supervised training
            target = self._teacher_action(perm_obs, ended)
            ml_loss += self.criterion(logit, target)

            # Determine next model inputs
            if self.feedback == 'teacher':
                a_t = target                 # teacher forcing
            elif self.feedback == 'argmax':
                _, a_t = logit.max(1)        # student forcing - argmax
                a_t = a_t.detach()
                log_probs = F.log_softmax(logit, 1)                              # Calculate the log_prob here
                policy_log_probs.append(log_probs.gather(1, a_t.unsqueeze(1)))   # Gather the log_prob for each batch
            elif self.feedback == 'sample':
                probs = F.softmax(logit, 1)  # sampling an action from model
                c = torch.distributions.Categorical(probs)
                self.logs['entropy'].append(c.entropy().sum().item())            # For log
                entropys.append(c.entropy())                                     # For optimization
                a_t = c.sample().detach()
                policy_log_probs.append(c.log_prob(a_t))
            else:
                print(self.feedback)
                sys.exit('Invalid feedback option')
            # Prepare environment action
            # NOTE: Env action is in the perm_obs space
            cpu_a_t = a_t.cpu().numpy()
     
            for i in range(len(candidate_feat_for_traj)):
                if cpu_a_t[i] == args.ignoreid:
                    continue
                index = cpu_a_t[i]+t
                if t > 0:
                    vis_taj[i,:t,:] = candidate_feat_for_traj[i,:t,:]

            for i, next_id in enumerate(cpu_a_t):
                if next_id == (candidate_leng[i]-1-t) or next_id == args.ignoreid or ended[i]:
                    cpu_a_t[i] = -1 
                else:
                    index = cpu_a_t[i] 
                    vis_taj[i, t, :self.feature_size] = visn_output[i, index, :]

            # Make action and get the new state
            self.make_equiv_action(cpu_a_t, perm_obs, perm_idx, traj)
            obs = np.array(self.env._get_obs())
            perm_obs = obs[perm_idx]            # Perm the obs for the resu

            if train_rl: # RL的部分，可以不用管，要用的时候直接copy
                # Calculate the mask and reward
                dist = np.zeros(batch_size, np.float32)
                ndtw_score = np.zeros(batch_size, np.float32)
                reward = np.zeros(batch_size, np.float32)
                mask = np.ones(batch_size, np.float32)
                for i, ob in enumerate(perm_obs):
                    dist[i] = ob['distance']
                    path_act = [vp[0] for vp in traj[i]['path']]
                    ndtw_score[i] = self.ndtw_criterion[ob['scan']](path_act, ob['gt_path'], metric='ndtw')

                    if ended[i]:
                        reward[i] = 0.0
                        mask[i] = 0.0
                    else:
                        action_idx = cpu_a_t[i]
                        # Target reward
                        if action_idx == -1:                              # If the action now is end
                            if dist[i] < 3.0:                             # Correct
                                reward[i] = 2.0 + ndtw_score[i] * 2.0
                            else:                                         # Incorrect
                                reward[i] = -2.0
                        else:                                             # The action is not end
                            # Path fidelity rewards (distance & nDTW)
                            reward[i] = - (dist[i] - last_dist[i])
                            ndtw_reward = ndtw_score[i] - last_ndtw[i]
                            if reward[i] > 0.0:                           # Quantification
                                reward[i] = 1.0 + ndtw_reward
                            elif reward[i] < 0.0:
                                reward[i] = -1.0 + ndtw_reward
                            else:
                                raise NameError("The action doesn't change the move")
                            # Miss the target penalty
                            if (last_dist[i] <= 1.0) and (dist[i]-last_dist[i] > 0.0):
                                reward[i] -= (1.0 - last_dist[i]) * 2.0
                rewards.append(reward)
                masks.append(mask)
                last_dist[:] = dist
                last_ndtw[:] = ndtw_score

            # Update the finished actions
            # -1 means ended or ignored (already ended)
            ended[:] = np.logical_or(ended, (cpu_a_t == -1))

            # Early exit if all ended
            if ended.all():
                break

        if train_rl: # 一个强化学习的代码，不用管
            # Last action in A2C
            input_a_t, candidate_feat, candidate_leng = self.get_input_feat(perm_obs, t+1, vis_taj)

            visual_temp_mask = (utils.length2mask(candidate_leng) == 0).long()
            visual_attention_mask = torch.cat((language_attention_mask, visual_temp_mask), dim=-1)

            ''' Visual BERT '''
            visual_inputs = {'mode':              'visual', # 此模式会综合考虑vision encoder 和 cross modal encoder
                            'sentence':           language_features,
                            'attention_mask':     visual_attention_mask,
                            'lang_mask':          language_attention_mask,
                            'vis_mask':           visual_temp_mask,
                            'token_type_ids':     token_type_ids,
                            'action_feats':       input_a_t,
                            # 'pano_feats':         f_t,
                            'cand_feats':         candidate_feat,#可以理解为未经过MLP的m_t包含了action_feats(即论文中的direction_feats) + vision_feats
                            't':                  t+1,
                            'seq_lengths':        seq_lengths,
                            'att_drop_rate':      att_drop_rate}
            last_h_, _, _, _ = self.vln_bert(**visual_inputs)

            rl_loss = 0.

            # NOW, A2C!!!
            # Calculate the final discounted reward
            last_value__ = self.critic(last_h_).detach()        # The value esti of the last state, remove the grad for safety
            discount_reward = np.zeros(batch_size, np.float32)  # The inital reward is zero
            for i in range(batch_size):
                if not ended[i]:        # If the action is not ended, use the value function as the last reward
                    discount_reward[i] = last_value__[i]

            length = len(rewards)
            total = 0
            for t in range(length-1, -1, -1):
                discount_reward = discount_reward * args.gamma + rewards[t]  # If it ended, the reward will be 0
                mask_ = Variable(torch.from_numpy(masks[t]), requires_grad=False).cuda()
                clip_reward = discount_reward.copy()
                r_ = Variable(torch.from_numpy(clip_reward), requires_grad=False).cuda()
                v_ = self.critic(hidden_states[t])
                a_ = (r_ - v_).detach()

                rl_loss += (-policy_log_probs[t] * a_ * mask_).sum()
                rl_loss += (((r_ - v_) ** 2) * mask_).sum() * 0.5  # 1/2 L2 loss
                if self.feedback == 'sample':
                    rl_loss += (- 0.01 * entropys[t] * mask_).sum()
                self.logs['critic_loss'].append((((r_ - v_) ** 2) * mask_).sum().item())

                total = total + np.sum(masks[t])
            self.logs['total'].append(total)

            # Normalize the loss function
            if args.normalize_loss == 'total':
                rl_loss /= total
            elif args.normalize_loss == 'batch':
                rl_loss /= batch_size
            else:
                assert args.normalize_loss == 'none'

            self.loss += rl_loss
            self.logs['RL_loss'].append(rl_loss.item())
        # 计算loss 对应论文中的(6)式
        if train_ml is not None:
            self.loss += ml_loss * train_ml / batch_size
            self.logs['IL_loss'].append((ml_loss * train_ml / batch_size).item())

        if train_dis_l is not None:
            self.loss += dis_loss_l * train_dis_l

        if train_dis_c is not None:
            self.loss += dis_loss_c * train_dis_c

        if type(self.loss) is int:  # For safety, it will be activated if no losses are added
            self.losses.append(0.)
        else:
            self.losses.append(self.loss.item() / self.episode_len)  # This argument is useless.

        return traj

    def test(self, use_dropout=False, feedback='argmax', allow_cheat=False, iters=None):
        ''' Evaluate once on each instruction in the current environment '''
        self.feedback = feedback
        if use_dropout:
            self.vln_bert.train()
            self.critic.train()
        else:
            self.vln_bert.eval()
            self.critic.eval()
        super(Seq2SeqAgent, self).test(iters)

    def zero_grad(self):
        self.loss = 0.
        self.losses = []
        for model, optimizer in zip(self.models, self.optimizers):
            model.train()
            optimizer.zero_grad()

    def optim_step(self):
        self.loss.backward()

        torch.nn.utils.clip_grad_norm(self.vln_bert.parameters(), 40.)

        self.vln_bert_optimizer.step()
        self.critic_optimizer.step()

    def train(self, n_iters, scaler, shared_models, shared_optimizers, feedback='teacher', **kwargs):

        ''' Train for a given number of iterations '''
        self.feedback = feedback

        self.vln_bert.train()
        self.critic.train()

        self.losses = []
        for iter in range(1, n_iters + 1):
            for model, shared_model in zip(self.models, shared_models):
                model.load_state_dict(shared_model.state_dict())

            self.vln_bert.zero_grad()
            self.critic.zero_grad()

            self.loss = 0
            with torch.cuda.amp.autocast():
                if feedback == 'teacher':
                    self.feedback = 'teacher'
                    self.rollout(train_ml=args.teacher_weight, train_dis_l=args.distance_weight, train_dis_c=args.distance_weight_c, att_drop_rate=args.drop_rate, train_rl=False, **kwargs)
                elif feedback == 'sample':  # agents in IL and RL separately
                    if args.ml_weight != 0:
                        self.feedback = 'teacher'
                        self.rollout(train_ml=args.ml_weight, train_dis_l=args.distance_weight, train_dis_c=args.distance_weight_c, att_drop_rate=args.drop_rate, train_rl=False, **kwargs)
                    self.feedback = 'sample'
                    self.rollout(train_ml=None, train_dis_l=args.distance_weight, train_dis_c=args.distance_weight_c, att_drop_rate=args.drop_rate, train_rl=True, **kwargs)
                else:
                    assert False

            scaler.scale(self.loss).backward()
            if args.aug is None:
                print_progress(iter, n_iters+1, prefix='Progress:', suffix='Complete', bar_length=50)

    def save(self, epoch, path):
        ''' Snapshot models '''
        the_dir, _ = os.path.split(path)
        os.makedirs(the_dir, exist_ok=True)
        states = {}
        def create_state(name, model, optimizer):
            states[name] = {
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
            }
        all_tuple = [("vln_bert", self.vln_bert, self.vln_bert_optimizer),
                     ("critic", self.critic, self.critic_optimizer)]
        for param in all_tuple:
            create_state(*param)
        torch.save(states, path)

    def load(self, path):
        ''' Loads parameters (but not training state) '''
        states = torch.load(path)

        def recover_state(name, model, optimizer):
            state = model.state_dict()
            model_keys = set(state.keys())
            load_keys = set(states[name]['state_dict'].keys())
            if model_keys != load_keys:
                print("NOTICE: DIFFERENT KEYS IN THE LISTEREN")
            state.update(states[name]['state_dict'])
            model.load_state_dict(state)
            if args.loadOptim:
                optimizer.load_state_dict(states[name]['optimizer'])
        all_tuple = [("vln_bert", self.vln_bert, self.vln_bert_optimizer),
                     ("critic", self.critic, self.critic_optimizer)]
        for param in all_tuple:
            recover_state(*param)
        return states['vln_bert']['epoch'] - 1
