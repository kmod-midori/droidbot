import sys
import json
import logging
import random
import time
import collections
import spacy
import copy
import numpy as np
from abc import abstractmethod

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence

from .input_event import InputEvent, KeyEvent, IntentEvent, TouchEvent, ManualEvent, SetTextEvent, KillAppEvent
from .input_policy import UtgBasedInputPolicy
from .device_state import DeviceState
from .utg import UTG

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-12s %(levelname)-8s %(message)s")


class Memory:
    def __init__(self, utg, app):
        self.utg = utg
        self.app = app
        self.model = self._build_model()
        self.known_states = collections.OrderedDict()
        self.known_transitions = collections.OrderedDict()
        self.nlp = spacy.load("en_core_web_md")

    def _build_model(self, embed_size=200):
        return torch.nn.LSTM(
            input_size=300,
            hidden_size=int(embed_size/2),
            num_layers=1,
            batch_first=True,
            bidirectional=True
        )

    def _memorize_state(self, state):
        if state.get_app_activity_depth(self.app) != 0:
            return None
        if state.state_str not in self.known_states:
            views = state.views
            views_str = [view['view_str'] for view in views]
            views_enc = torch.stack([self._encode_view(view) for view in views])
            embedder = self.model
            embedder.eval()
            with torch.no_grad():
                views_emb, _ = self.model(views_enc.unsqueeze(0))
                views_emb = views_emb.detach().cpu()[0]
            self.known_states[state.state_str] = {
                'state': state,
                'views': views,
                'views_str': views_str,
                'views_enc': views_enc,
                'views_emb': views_emb
            }
        return self.known_states[state.state_str]

    def _encode_view(self, view):
        # print(view)
        return torch.zeros(300)
        # is_parent
        # is_image
        # is_text
        # clickable
        # long_clickable
        # checkable
        # editable
        # scrollable
        # size
        # wh_ratio
        # text
        # encoding = [is_parent, is_image, is_text, clickable, long_clickable, checkable, editable, scrollable, size, wh_ratio]
    
    def _update_known_transitions(self):
        for from_state, action, to_state in self.utg.transitions:
            if not isinstance(action, TouchEvent):
                continue
            if action.view is None:
                continue
            action_str = action.get_event_str(state=from_state)
            if action_str in self.known_transitions and self.known_transitions[action_str]['to_state'] == to_state:
                continue
            state_info = self._memorize_state(from_state)
            if state_info is None:
                continue
            view = action.view
            view_idx = state_info['views_str'].index(view['view_str'])
            action_effect = f'{from_state.structure_str}->{to_state.structure_str}'
            self.known_transitions[action_str] = {
                'from_state': from_state,
                'to_state': to_state,
                'action': action,
                'view_idx': view_idx,
                'action_effect': action_effect
            }

    def encode_action_pairs(self, action_strs=None):
        if action_strs is None:
            action_strs = list(self.known_transitions.keys())
        if len(action_strs) < 2:
            return

        state_strs = [self.known_transitions[action_str]['from_state'].state_str for action_str in action_strs]
        state_encs = [self.known_states[state_str]['views_enc'] for state_str in state_strs]
        action_pairs = []
        for i, action_str1 in enumerate(action_strs):
            state_str1 = self.known_transitions[action_str1]['from_state'].state_str
            state_idx1 = state_strs.index(state_str1)
            view_idx1 = self.known_transitions[action_str1]['view_idx']
            for j, action_str2 in enumerate(action_strs[i+1:]):
                state_str2 = self.known_transitions[action_str2]['from_state'].state_str
                state_idx2 = state_strs.index(state_str2)
                view_idx2 = self.known_transitions[action_str2]['view_idx']
                action_effect1 = self.known_transitions[action_str1]['action_effect']
                action_effect2 = self.known_transitions[action_str2]['action_effect']
                effect_same = 1 if action_effect1 == action_effect2 else 0
                action_pairs.append((state_idx1, view_idx1, state_idx2, view_idx2, effect_same))
        return state_encs, action_pairs
    
    def get_known_actions_emb(self):
        actions_emb = []
        for action_str in self.known_transitions:
            action_info = self.known_transitions[action_str]
            state_str = action_info['from_state'].state_str
            view_idx = action_info['view_idx']
            action_emb = self.known_states[state_str]['views_emb'][view_idx]
            actions_emb.append(action_emb)
        return torch.stack(actions_emb)

    def train_model(self):
        self._update_known_transitions()
        # print(self.known_transitions)

        embedder = self.model
        optimizer = torch.optim.Adam(embedder.parameters(), lr=1e-3)
        n_iterations = 5

        def compute_loss(ele_embed, action_pairs):
            pos_emb_u = []
            pos_emb_v = []
            neg_emb_u = []
            neg_emb_v = []
            for state_idx1, view_idx1, state_idx2, view_idx2, effect_same in action_pairs:
                emb_u = ele_embed[state_idx1, view_idx1]
                emb_v = ele_embed[state_idx2, view_idx2]
                if effect_same:
                    pos_emb_u.append(emb_u)
                    pos_emb_v.append(emb_v)
                else:
                    neg_emb_u.append(emb_u)
                    neg_emb_v.append(emb_v)
            pos_emb_u = torch.stack(pos_emb_u)
            pos_emb_v = torch.stack(pos_emb_v)
            neg_emb_u = torch.stack(neg_emb_u)
            neg_emb_v = torch.stack(neg_emb_v)
            # print(f'{emb_u.size()} {emb_v.size()} {ele_embed.size()}')

            pos_score = torch.cosine_similarity(pos_emb_u, pos_emb_v)
            pos_score = F.logsigmoid(pos_score).mean()

            neg_score = torch.cosine_similarity(neg_emb_u, neg_emb_v)
            neg_score = F.logsigmoid(-neg_score).mean()

            loss = -pos_score - neg_score
            return loss

        def train():
            embedder.train()
            state_encs, action_pairs = self.encode_action_pairs()
            state_encs = pad_sequence(state_encs, batch_first=True)
            ele_embed, _ = embedder(state_encs)

            loss = compute_loss(ele_embed, action_pairs)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            return loss.item(), len(action_pairs)

        for i in range(n_iterations):
            epoch_start_time = time.time()
            loss, n_pairs = train()
            elapsed = time.time() - epoch_start_time
            print(f'| iter: {i:3d} | time: {elapsed:8.2f}s | #pairs: {n_pairs:6d} | loss: {loss:8.4f}')
        
    def get_unexplored_actions(self, current_state):
        action_strs = set()
        self._memorize_state(current_state)
        for state_str, state_info in reversed(self.known_states.items()):
            state = state_info['state']
            for action in state.get_possible_input():
                if not isinstance(action, TouchEvent):
                    continue
                action_str = action.get_event_str(state=state)
                if action_str in action_strs:
                    continue
                if self.utg.is_event_explored(action, state):
                    continue
                action_strs.add(action_str)
                yield state, action

    def get_action_emb(self, state, action):
        state_str = state.state_str
        view_str = action.view['view_str']
        view_idx = self.known_states[state_str]['views_str'].index(view_str)
        action_emb = self.known_states[state_str]['views_emb'][view_idx]
        return action_emb


# Max number of steps outside the app
MAX_NUM_STEPS_OUTSIDE = 3
MAX_NUM_STEPS_OUTSIDE_KILL = 5
MAX_NAV_STEPS = 10


class MemoryGuidedPolicy(UtgBasedInputPolicy):
    def __init__(self, device, app, random_input):
        super(MemoryGuidedPolicy, self).__init__(device, app, random_input)
        self.logger = logging.getLogger(self.__class__.__name__)

        self.random_explore_prob = 0.2
        self.memory = Memory(utg=self.utg, app=self.app)
        self.num_actions_train = 10

        self._nav_steps = []
        self._num_steps_outside = 0

    def generate_event_based_on_utg(self):
        """
        generate an event based on current UTG
        @return: InputEvent
        """
        if self.action_count % self.num_actions_train == 0:
            self.memory.train_model()

        current_state = self.current_state
        if self.last_event is not None:
            self.last_event.log_lines = self.parse_log_lines()
        # interested_apis = self.monitor.get_interested_api()
        # self.monitor.check_env()
        self.logger.debug("Current state: %s" % current_state.state_str)

        if current_state.get_app_activity_depth(self.app) < 0:
            # If the app is not in the activity stack
            start_app_intent = self.app.get_start_intent()
            self.logger.info("starting app")
            return IntentEvent(intent=start_app_intent)
        elif current_state.get_app_activity_depth(self.app) > 0:
            # If the app is in activity stack but is not in foreground
            self._num_steps_outside += 1
            if self._num_steps_outside > MAX_NUM_STEPS_OUTSIDE:
                # If the app has not been in foreground for too long, try to go back
                if self._num_steps_outside > MAX_NUM_STEPS_OUTSIDE_KILL:
                    stop_app_intent = self.app.get_stop_intent()
                    go_back_event = IntentEvent(stop_app_intent)
                else:
                    start_app_intent = self.app.get_start_intent()
                    go_back_event = IntentEvent(intent=start_app_intent)
                self.logger.info("going back to the app")
                return go_back_event
        else:
            # If the app is in foreground
            self._num_steps_outside = 0

        if self.action_count >= self.num_actions_train \
                and len(self._nav_steps) == 0 \
                and np.random.uniform() > self.random_explore_prob:
            target_state, target_action = self.pick_target(current_state)
            # perform target action or navigate to target action
            if target_state.state_str == current_state.state_str:
                self.logger.info(f"executing selected action")
                return target_action
            self._nav_steps = self.get_shortest_nav_steps(current_state, target_state, target_action)

        if self._nav_steps and len(self._nav_steps) > 0:
            nav_state, nav_action = self._nav_steps[0]
            self._nav_steps = self._nav_steps[1:]
            self.logger.info(f"navigating, {len(self._nav_steps)} steps left")
            return nav_action
        self._nav_steps = []  # if navigation fails, stop navigating

        self.logger.info("trying random action")
        possible_events = current_state.get_possible_input()
        possible_events.append(KeyEvent(name="BACK"))
        random.shuffle(possible_events)
        return possible_events[0]

    def pick_target(self, current_state):
        state_action_pairs = self.memory.get_unexplored_actions(current_state)
        best_target = None, None
        best_score = -np.inf
        known_actions_emb = self.memory.get_known_actions_emb()
        for state, action in state_action_pairs:
            action_emb = self.memory.get_action_emb(state, action)
            similarities = torch.cosine_similarity(action_emb.repeat((known_actions_emb.size(0), 1)), known_actions_emb)
            max_sim, max_sim_idx = similarities.max(0)
            score = -max_sim
            if state.state_str == current_state.state_str:
                score += 0.1        # encourage actions in current state
            if score > best_score:
                best_score = score
                best_target = state, action
        return best_target

    def _get_nav_action(self, current_state, nav_state, nav_action):
        # get the action similar to nav_action in current state
        try:
            if current_state.structure_str != nav_state.structure_str:
                return None
            nav_view = nav_action.view
            nav_view_idx = nav_state.views.index(nav_view)
            new_view = current_state.views[nav_view_idx]
            new_action = copy.deepcopy(nav_action)
            new_action.view = new_view
            return new_action
        except Exception as e:
            self.logger.warning(f'exception during _get_nav_action: {e}')
            return nav_action

    def parse_log_lines(self):
        log_lines = self.device.logcat.get_recent_lines()
        filtered_lines = []
        app_pid = self.device.get_app_pid(self.app)
        # print(f'current app_pid: {app_pid}')
        for line in log_lines:
            try:
                seps = line.split()
                if int(seps[2]) == app_pid:
                    filtered_lines.append(line)
            except:
                pass
        return filtered_lines

    def get_shortest_nav_steps(self, current_state, target_state, target_action):
        normal_nav_steps = self.utg.get_navigation_steps(current_state, target_state)
        restart_nav_steps = self.utg.get_navigation_steps(self.utg.first_state, target_state)
        normal_nav_steps_len = len(normal_nav_steps) if normal_nav_steps else MAX_NAV_STEPS
        restart_nav_steps_len = len(restart_nav_steps) + 1 if restart_nav_steps else MAX_NAV_STEPS
        if normal_nav_steps_len >= MAX_NAV_STEPS and restart_nav_steps_len >= MAX_NAV_STEPS:
            return None
        elif normal_nav_steps_len > restart_nav_steps_len:
            nav_steps = [(current_state, KillAppEvent(app=self.app))] + restart_nav_steps
        else:
            nav_steps = normal_nav_steps
        return nav_steps + [(target_state, target_action)]


# class InputPolicy2(object):
#     """
#     This class is responsible for generating events to stimulate more app behaviour
#     """
#
#     def __init__(self, device, app, random_input=True):
#         self.logger = logging.getLogger(self.__class__.__name__)
#         self.device = device
#         self.app = app
#         self.random_input = random_input
#         self.utg = UTG(device=device, app=app, random_input=random_input)
#         self.input_manager = None
#         self.action_count = 0
#         self.state = None
#
#     @property
#     def enabled(self):
#         if self.input_manager is None:
#             return False
#         return self.input_manager.enabled and self.action_count < self.input_manager.event_count
#
#     def perform_action(self, action):
#         self.input_manager.add_event(action)
#         self.action_count += 1
#
#     def start(self, input_manager):
#         """
#         start producing actions
#         :param input_manager: instance of InputManager
#         """
#         self.input_manager = input_manager
#         self.action_count = 0
#
#         episode_i = 0
#         while self.enabled:
#             try:
#                 episode_i += 1
#                 self.device.send_intent(self.app.get_stop_intent())
#                 self.device.key_press('HOME')
#                 self.device.send_intent(self.app.get_start_intent())
#                 self.state = self.device.current_state()
#                 self.start_episode()
#             except KeyboardInterrupt:
#                 break
#             except Exception as e:
#                 self.logger.warning(f"exception during episode {episode_i}: {e}")
#                 import traceback
#                 traceback.print_exc()
#                 continue
#
#     @abstractmethod
#     def start_episode(self):
#         pass

