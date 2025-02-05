import numpy as np
import os
import random
import pickle
import imageio
from scipy.spatial.transform import Rotation as R

from importlib import import_module
from utils.torch_jit_utils import *
from utils.util import world2screen, cam2world, compute_camera_transform
from tasks.base.base_task import BaseTask
from isaacgym import gymtorch
from isaacgym import gymapi
from isaacgym import gymutil
import torch

from termcolor import colored, cprint

class CloseDoorOutward(BaseTask):

    def __init__(self, cfg, sim_params, physics_engine, device_type, device_id, camera, headless):
        self.cfg = cfg
        self.sim_params = sim_params
        self.physics_engine = physics_engine
        self.camera = camera
        self.is_planning = cfg.get('planning', False)
        # init repre_type for initial the robot
        self.repre_type = self.cfg["repre"]["type"]
        if self.repre_type == 'eureka':
            self.eureka_seed = self.cfg["repre"]["seed"]%3
        self.act_type = self.cfg["env"]["actionType"]

        self.randomize = self.cfg["task"]["randomize"]
        self.randomization_params = self.cfg["task"]["randomization_params"]
        self.aggregate_mode = self.cfg["env"]["aggregateMode"]

        self.dist_reward_scale = self.cfg["env"]["distRewardScale"]
        self.rot_reward_scale = self.cfg["env"]["rotRewardScale"]

        #? looks like all following params about reward setting
        self.dist_reward_scale = self.cfg["env"]["distRewardScale"]
        self.rot_reward_scale = self.cfg["env"]["rotRewardScale"]
        self.action_penalty_scale = self.cfg["env"]["actionPenaltyScale"]
        self.success_tolerance = self.cfg["env"]["successTolerance"]
        self.reach_goal_bonus = self.cfg["env"]["reachGoalBonus"]
        self.fall_dist = self.cfg["env"]["fallDistance"]
        self.fall_penalty = self.cfg["env"]["fallPenalty"]
        self.rot_eps = self.cfg["env"]["rotEps"]

        self.vel_obs_scale = 0.2  # scale factor of velocity based observations
        self.force_torque_obs_scale = 10.0  # scale factor of velocity based observations, #* not used in this task

        #* following params about reset noise, could be removed in manipulation task setting
        self.reset_position_noise = self.cfg["env"]["resetPositionNoise"]
        self.reset_rotation_noise = self.cfg["env"]["resetRotationNoise"]
        self.reset_dof_pos_noise = self.cfg["env"]["resetDofPosRandomInterval"]
        self.reset_dof_vel_noise = self.cfg["env"]["resetDofVelRandomInterval"]

        #* following params about agent control setting
        self.robot_dof_speed_scale = self.cfg["env"]["dofSpeedScale"]
        self.attractor_speed_scale = self.cfg["env"]["attractorSpeedScale"]
        self.use_relative_control = self.cfg["env"]["useRelativeControl"]
        self.act_moving_average = self.cfg["env"]["actionsMovingAverage"]

        #? unknown params, default is False
        self.debug_viz = self.cfg["env"]["enableDebugVis"]

        #* following params about RL episode setting
        self.max_episode_length = self.cfg["env"]["episodeLength"]
        self.reset_time = self.cfg["env"].get("resetTime", -1.0)
        self.print_success_stat = self.cfg["env"]["printNumSuccesses"]
        self.max_consecutive_successes = self.cfg["env"]["maxConsecutiveSuccesses"]
        self.av_factor = self.cfg["env"].get("averFactor", 0.01)
        print("Averaging factor: ", self.av_factor)

        #* set control freq and reset time
        control_freq_inv = self.cfg["env"].get("controlFrequencyInv", 1)
        if self.reset_time > 0.0:
            self.max_episode_length = int(round(self.reset_time/(control_freq_inv * self.sim_params.dt)))
            print("Reset time: ", self.reset_time)
            print("New episode length: ", self.max_episode_length)

        self.up_axis = 'z'
        
        #* set observation setting
        # robot_state observation denotes the action space of the robot
        # full_state observation denotes the action space of the robot and the object
        self.obs_type = self.cfg["env"]["observationType"]
        if not (self.obs_type in ["robot_state", "full_state", "visual_repre"]):
            raise Exception(
                "Unknown type of observations!\nobservationType should be one of: [robot_state, full_state]")
        print("Obs type:", self.obs_type)
        if self.act_type in ['robot_joint_pose']:
            self.num_obs_dict = {
                "full_state": 20 * 2 + 1,
            }
        else:
            self.num_obs_dict = {
                "full_state": 8 * 2 + 1 + 1 + 1,  # num_dofs = 8
            }
        self.cfg["env"]["numObservations"] = self.num_obs_dict[self.obs_type]
        if self.cfg["env"]["numObservations"] is None:
            raise NotImplementedError("Not implemented observation type: ", self.obs_type)

        #* set action setting
        #* IK control remains to be completed
        if self.act_type == "robot_joint_pose":
            self.cfg["env"]["numActions"] = 2 * (7 + 2)
        elif self.act_type == "end_effector_pose":
            # assert False, "Not implemented end effector pose action"
            self.cfg["env"]["numActions"] = 2 * (3 + 4 + 1)  # 3 for position, 4 for quat orientation, 1 for gripper action
        elif self.act_type == 'dummy_interaction':
            self.cfg["env"]["numActions"] = 2 *((6 + 2) + 3)
        elif self.act_type == 'dummy_interaction_sphere':
            self.cfg["env"]["numActions"] = 2 * (3 + 3 + 1) # 3 for sphere position, 3 for dummy force direction, 1 for dummy force magnitude
        else:
            raise Exception("Unknown action type: ", self.act_type)

        #* set gym device and headless mode
        self.cfg["device_type"] = device_type
        self.cfg["device_id"] = device_id
        self.cfg["headless"] = headless

        #? unknown params, default is True (in  task config.yaml)
        self.camera_debug = self.cfg["env"].get("cameraDebug", False)

        if self.repre_type in ["handcrafted", "eureka"]:
            super().__init__(cfg=self.cfg)
        else:
            super().__init__(cfg=self.cfg, enable_camera_sensors=True)

        #* set camera pose for visualization
        if self.viewer != None:
            cam_pos = gymapi.Vec3(1.0, 0.0, 0.5)
            cam_target = gymapi.Vec3(0.0, 0.0, 0.5)
            self.gym.viewer_camera_look_at(self.viewer, None, cam_pos, cam_target)
        
        #* get gym GPU state tensors
        actor_root_state_tensor = self.gym.acquire_actor_root_state_tensor(self.sim)
        dof_state_tensor = self.gym.acquire_dof_state_tensor(self.sim)
        rigid_body_tensor = self.gym.acquire_rigid_body_state_tensor(self.sim)
        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_dof_state_tensor(self.sim)
        self.gym.refresh_rigid_body_state_tensor(self.sim)

        #* create some wrapper tensors for different slices
        # self.robot_default_dof_pos = torch.zeros(self.num_robot_dofs, dtype=torch.float, device=self.device)
        self.dof_states = gymtorch.wrap_tensor(dof_state_tensor).view(self.num_envs, -1, 2)  # [B, 8, 2]
        self.robot_dof_state = self.dof_states.view(self.num_envs, -1, 2)[:, :self.num_robot_dofs]  # num_robot_dofs=3
        self.robot_dof_pos = self.robot_dof_state[..., 0]
        self.robot_dof_vel = self.robot_dof_state[..., 1]

        self.robot_another_dof_state = self.dof_states.view(self.num_envs, -1, 2)[:, self.num_robot_dofs:2*self.num_robot_dofs]
        self.robot_another_dof_pos = self.robot_another_dof_state[..., 0]
        self.robot_another_dof_vel = self.robot_another_dof_state[..., 1]

        self.rigid_body_states = gymtorch.wrap_tensor(rigid_body_tensor).view(self.num_envs, -1, 13)
        self.num_bodies = self.rigid_body_states.shape[1]  # 12
        self.root_state_tensor = gymtorch.wrap_tensor(actor_root_state_tensor).view(self.num_envs, -1, 13)  # B, 3, 13
        self.root_start_state_tensor = gymtorch.wrap_tensor(actor_root_state_tensor).view(self.num_envs, -1, 13).clone()
        self.dof_start_state_tensor = gymtorch.wrap_tensor(dof_state_tensor).view(self.num_envs, -1, 2).clone()
        self.num_dofs = self.gym.get_sim_dof_count(self.sim) // self.num_envs
        self.prev_targets = torch.zeros((self.num_envs, self.num_dofs), dtype=torch.float, device=self.device)
        self.cur_targets = torch.zeros((self.num_envs, self.num_dofs), dtype=torch.float, device=self.device)
        self.obs_buf = torch.zeros((self.num_envs, self.num_obs_dict[self.obs_type]), dtype=torch.float, device=self.device)

        if self.act_type in ['end_effector_pose']:
            self.attractor_cur_targets = torch.zeros((self.num_envs, 3 + 4), dtype=torch.float, device=self.device)
            self.attractor_prev_targets = torch.zeros((self.num_envs, 3 + 4), dtype=torch.float, device=self.device)
            self.attractor_another_cur_targets = torch.zeros((self.num_envs, 3 + 4), dtype=torch.float, device=self.device)
            self.attractor_another_prev_targets = torch.zeros((self.num_envs, 3 + 4), dtype=torch.float, device=self.device)
        #? unkonwn variables
        self.global_indices = torch.arange(self.num_envs * 3, dtype=torch.int32, device=self.device).view(self.num_envs, -1)
        self.x_unit_tensor = to_torch([1, 0, 0], dtype=torch.float, device=self.device).repeat((self.num_envs, 1))
        self.y_unit_tensor = to_torch([0, 1, 0], dtype=torch.float, device=self.device).repeat((self.num_envs, 1))
        self.z_unit_tensor = to_torch([0, 0, 1], dtype=torch.float, device=self.device).repeat((self.num_envs, 1))
    
        self.reset_goal_buf = self.reset_buf.clone()
        self.successes = torch.zeros(self.num_envs, dtype=torch.float, device=self.device)
        self.consecutive_successes = torch.zeros(1, dtype=torch.float, device=self.device)
        self.success_scores = torch.zeros(self.num_envs, dtype=torch.float, device=self.device)
        self.process_buf = torch.zeros(self.num_envs, dtype=torch.int, device=self.device)
        self.reset_buf = torch.zeros(self.num_envs, dtype=torch.bool, device=self.device)

        #? unkonwn variable av_factor
        self.av_factor = to_torch(self.av_factor, dtype=torch.float, device=self.device)

        self.total_successes = 0
        self.total_resets = 0

        #* specify the representation for shaping reward
        # self.repre_type = self.cfg["repre"]["type"]
        # set config
        task_envname = self.__class__.__module__.split(".")[1]
        task_skillname = self.__class__.__name__

        if self.repre_type in ['r3m', 'vip']:
            goal_path = f"./tasks/{task_envname}/goals_image/{task_skillname}@{self.camera}@woa.png"
            goal_image = imageio.imread(goal_path)
            goal_image = torch.tensor(goal_image.astype(np.float32) / 255.0, dtype=torch.float32, device=self.device)
        elif self.repre_type in ['ag2x2']:
            goal_path = f"./tasks/{task_envname}/goals_image/{task_skillname}@{self.camera}@woa.png"
            goal_image = imageio.imread(goal_path)
            goal_image = torch.tensor(goal_image.astype(np.float32) / 255.0, dtype=torch.float32, device=self.device)
        elif self.repre_type in ['handcrafted', 'eureka']:
            pass
        else:
            raise NotImplementedError("Not implemented representation type: ", self.repre_type)
        if self.repre_type in ['r3m', 'vip', 'ag2x2']: 
            goal_hand = world2screen(torch.tensor([[0.09, 0.07, 0.5], [0.09, -0.07, 0.5]]).unsqueeze(0).to(self.device),
                    self.view_m, self.projection_m,
                    self.camera_w)  # [1,2,3] -> [1,2,2]
            cfg_repre = cfg['repre']   
            cfg_repre.update({
                "goal_image": goal_image,
                "goal_hand" : goal_hand,
                "batchsize": self.cfg["repre"]["batchsize"] if self.cfg["repre"]["batchsize"] < self.num_envs else self.num_envs,
                "device": self.device,})
        elif self.repre_type in ['handcrafted', 'eureka']:
            cfg_repre = {}
        else:
            raise NotImplementedError("Not implemented representation type: ", self.repre_type)
        
        #* init representation model
        if self.repre_type in ['r3m', 'vip', 'ag2x2']: 
            repre_model_name = self.cfg["repre"]["model"]
            Module = import_module(f"repres.{repre_model_name.lower()}")
            RepreModel = getattr(Module, repre_model_name)
            self.repre_model = RepreModel(cfg_repre)
            self.initial_value = None
        elif self.repre_type in []:
            #! load the pretrained checkpoint
            raise NotImplementedError("Not implemented loading pretrained checkpoint")
        elif self.repre_type in ['handcrafted', 'eureka']:
            self.repre_model = None
            self.repre_checkpoints = None
        else:
            raise NotImplementedError("Not implemented representation type: ", self.repre_type)
        
        #* init control-stage
        self._setup_attachable_body()
        self.extras['max_consecutive_successes'] = torch.zeros(1, dtype=torch.float, device=self.device)
        self.extras['max_success_scores'] = torch.zeros(1, dtype=torch.float, device=self.device)

    def create_sim(self):
        """
        Allocates which device will simulate and which device will render the scene. Defines the simulation type to be used
        """
        self.dt = self.sim_params.dt
        self.up_axis_idx = self.set_sim_params_up_axis(self.sim_params, self.up_axis)

        self.sim = super().create_sim(self.device_id, self.graphics_device_id, self.physics_engine, self.sim_params)
        self._create_ground_plane()
        self._set_lighting()
        if self.repre_type in ['handcrafted', 'eureka']:
            self._create_envs(self.num_envs, 10., int(np.sqrt(self.num_envs)))
        else:
            self._create_envs(self.num_envs, self.cfg["env"]['envSpacing'], int(np.sqrt(self.num_envs)))

    def _set_lighting(self):
        """
        Sets lighting for the scene
        """
        light_index = 0
        intensity = gymapi.Vec3(0.1, 0.1, 0.1) * 5.
        ambient = gymapi.Vec3(0.1, 0.1, 0.1) * 10
        direction = gymapi.Vec3(1.0, -1.0, 1.0)
        self.gym.set_light_parameters(self.sim, light_index, intensity, ambient, direction)

    def _create_ground_plane(self):
        """
        Adds ground plane to simulation
        """
        plane_params = gymapi.PlaneParams()
        plane_params.normal = gymapi.Vec3(0.0, 0.0, 1.0)
        plane_params.distance = 0.0
        self.gym.add_ground(self.sim, plane_params)
    
    def _create_envs(self, num_envs, spacing, num_per_row):
        """
        Create multiple parallel isaacgym environments

        Args:
            num_envs (int): The total number of environment 

            spacing (float): Specifies half the side length of the square area occupied by each environment

            num_per_row (int): Specify how many environments in a row
        """
        lower = gymapi.Vec3(-spacing, -spacing, 0.0)
        upper = gymapi.Vec3(spacing, spacing, spacing)

        cfg_asset = self.cfg["env"]["asset"]

        robot_asset_root = cfg_asset["agent"]["assetRoot"]
        print(f'repre type: {self.repre_type}')
        if self.is_planning:
            robot_asset_file = cfg_asset["agent"]["franka"]
            robot_another_asset_file = cfg_asset["agent"]["franka1"]
        elif self.repre_type in ['handcrafted', 'eureka']:
            if self.act_type == 'dummy_interaction_sphere':
                robot_asset_file = cfg_asset["agent"]["sphere-wvis"]
                robot_another_asset_file = cfg_asset["agent"]["sphere-wvis1"]
            elif self.act_type == 'robot_joint_pose':
                robot_asset_file = cfg_asset["agent"]["franka"]
                robot_another_asset_file = cfg_asset["agent"]["franka1"]
            else:
                raise NotImplementedError
        elif self.repre_type in ['ag2x2', 'r3m', 'vip']:
            if self.act_type == 'dummy_interaction_sphere':
                robot_asset_file = cfg_asset["agent"]["sphere-wovis"]
                robot_another_asset_file = cfg_asset["agent"]["sphere-wovis1"]
            elif self.act_type == 'robot_joint_pose':
                robot_asset_file = cfg_asset["agent"]["franka-wovis"]
                robot_another_asset_file = cfg_asset["agent"]["franka-wovis1"]
            else:
                raise NotImplementedError
        else:
            raise NotImplementedError("Not implemented representation type on **robot asset file**: ", self.repre_type)

        object_asset_root = cfg_asset["object"]["assetRoot"]
        object_asset_files = {
            k: v for k, v in cfg_asset["object"].items()
        }

        #* load franka robot asset from file
        asset_options = gymapi.AssetOptions()
        asset_options.fix_base_link = True
        asset_options.flip_visual_attachments = True
        asset_options.use_mesh_materials = True
        asset_options.collapse_fixed_joints = True
        if self.is_planning:
            asset_options.collapse_fixed_joints = False
        #? not sure about the impact of these following terms
        asset_options.disable_gravity = True  
        asset_options.thickness = 0.001
        asset_options.angular_damping = 0.01

        if self.physics_engine == gymapi.SIM_PHYSX:
            asset_options.use_physx_armature = True
        asset_options.default_dof_drive_mode = gymapi.DOF_MODE_POS

        robot_asset = self.gym.load_asset(self.sim, robot_asset_root, robot_asset_file, asset_options)
        robot_another_asset = self.gym.load_asset(self.sim, robot_asset_root, robot_another_asset_file, asset_options)

        self.num_robot_bodies = self.gym.get_asset_rigid_body_count(robot_asset)  # 4
        self.num_robot_shapes = self.gym.get_asset_rigid_shape_count(robot_asset)  # 1
        self.num_robot_dofs = self.gym.get_asset_dof_count(robot_asset)  # 3
        self.num_robot_actuators = self.gym.get_asset_actuator_count(robot_asset)
        self.num_robot_tendons = self.gym.get_asset_tendon_count(robot_asset)

        print("self.num_robot_bodies: ", self.num_robot_bodies)
        print("self.num_robot_shapes: ", self.num_robot_shapes)
        print("self.num_robot_dofs: ", self.num_robot_dofs)
        if self.act_type in ['end_effector_pose']:
            assert self.num_robot_dofs == 9
        print("self.num_robot_actuators: ", self.num_robot_actuators)
        print("self.num_robot_tendons: ", self.num_robot_tendons)

        #* load object assets from file
        self.num_object_dofs = 0
        self.num_object_bodies = 0
        self.num_object_shapes = 0
        object_assets = {}
        for object_asset_name, object_asset_file in object_asset_files.items():
            if object_asset_name == "assetRoot":
                continue
            object_asset_options = gymapi.AssetOptions()
            object_asset_options = gymapi.AssetOptions()
            object_asset_options.density = 1000
            object_asset_options.fix_base_link = True
            object_asset_options.disable_gravity = True
            object_asset_options.use_mesh_materials = True
            object_asset_options.mesh_normal_mode = gymapi.COMPUTE_PER_VERTEX
            object_asset_options.override_com = True
            object_asset_options.override_inertia = True
            object_asset_options.vhacd_enabled = True
            object_asset_options.vhacd_params = gymapi.VhacdParams()
            object_asset_options.vhacd_params.resolution = 200000
            # asset_options.convex_decomposition_from_submeshes = True
            object_asset_options.linear_damping = 10
            object_asset_options.default_dof_drive_mode = gymapi.DOF_MODE_NONE

            object_assets[object_asset_name] = self.gym.load_asset(self.sim, object_asset_root, object_asset_file, object_asset_options)
            self.num_object_dofs += self.gym.get_asset_dof_count(object_assets[object_asset_name])  # 2
            self.num_object_bodies += self.gym.get_asset_rigid_body_count(object_assets[object_asset_name])  # 4
            self.num_object_shapes += self.gym.get_asset_rigid_shape_count(object_assets[object_asset_name])  # 20
        # variables for setting up the aggreaget mode
        max_agg_bodies = self.num_robot_bodies * 2 + self.num_object_bodies + 1
        max_agg_shapes = self.num_robot_shapes * 2 + self.num_object_shapes + 1

        robot_dof_props = self.gym.get_asset_dof_properties(robot_asset)
        robot_another_dof_props = self.gym.get_asset_dof_properties(robot_another_asset)
        
        if self.act_type in ['end_effector_pose']:
            robot_dof_props['stiffness'].fill(400.)
            robot_dof_props['damping'].fill(40.)
            robot_dof_props['driveMode'][7:] = gymapi.DOF_MODE_EFFORT
            robot_dof_props['driveMode'][0:7] = gymapi.DOF_MODE_NONE
            robot_another_dof_props['stiffness'].fill(400.)
            robot_another_dof_props['damping'].fill(40.)
            robot_another_dof_props['driveMode'][7:] = gymapi.DOF_MODE_EFFORT
            robot_another_dof_props['driveMode'][0:7] = gymapi.DOF_MODE_NONE
        elif self.act_type in ['robot_joint_pose']:
            robot_dof_props['driveMode'][:] = gymapi.DOF_MODE_POS
            robot_another_dof_props['driveMode'][:] = gymapi.DOF_MODE_POS
        
        self.robot_dof_lower_limits = []
        self.robot_dof_upper_limits = []
        self.robot_dof_default_pos = []
        self.robot_dof_default_vel = []

        for i in range(self.num_robot_dofs):
            self.robot_dof_lower_limits.append(robot_dof_props["lower"][i])
            self.robot_dof_upper_limits.append(robot_dof_props["upper"][i])
            self.robot_dof_default_vel.append(0.)
            
        self.robot_dof_lower_limits = to_torch(self.robot_dof_lower_limits, device=self.device)
        self.robot_dof_upper_limits = to_torch(self.robot_dof_upper_limits, device=self.device)

        #! set goal dof if saving goal images
        robot_start_dof = (self.robot_dof_upper_limits + self.robot_dof_lower_limits) * 0.5
        if self.is_planning or self.act_type in ['robot_joint_pose']:
            robot_start_dof = to_torch([0.0, -0.785, 0.0, -2.356, 0.0, 1.571, 0.785, 0.04, 0.04], device=self.device)
        self.robot_dof_default_pos = robot_start_dof
        self.robot_dof_default_pos = to_torch(self.robot_dof_default_pos, device=self.device)
        self.robot_dof_default_vel = to_torch(self.robot_dof_default_vel, device=self.device)
        
        #* set robot base pose
        if self.is_planning or self.act_type in ['robot_joint_pose']:
            # raise NotImplementedError
            robot_start_pose = gymapi.Transform()
            robot_start_pose.p = gymapi.Vec3(0.4, 0.8, 0.0)  # right
            robot_start_pose.r = gymapi.Quat(*R.from_euler('xyz', [0., 0., -0.3]).as_quat())
            robot_another_start_pose = gymapi.Transform()
            robot_another_start_pose.p = gymapi.Vec3(0.4, -0.8, 0.0)  # left
            robot_another_start_pose.r = gymapi.Quat(*R.from_euler('xyz', [0., 0., 0.3]).as_quat())
        else:
            robot_start_pose = gymapi.Transform()
            #robot_start_pose.p = gymapi.Vec3(1.0, 0.5, 0.8)  # right
            #robot_start_pose.p = gymapi.Vec3(0.45, 0.45, 0.8)
            robot_start_pose.p = gymapi.Vec3(0.45, 0.7, 0.5)
            robot_start_pose.r = gymapi.Quat(*R.from_euler('xyz', [0., 0., 0.]).as_quat())
            robot_another_start_pose = gymapi.Transform()
            #robot_another_start_pose.p = gymapi.Vec3(1.0, -0.5, 0.8)  # left
            #robot_another_start_pose.p = gymapi.Vec3(0.45, -0.45, 0.8)
            robot_another_start_pose.p = gymapi.Vec3(0.45, -0.7, 0.5)
            robot_another_start_pose.r = gymapi.Quat(*R.from_euler('xyz', [0., 0., 0.]).as_quat())
        #* set object base pose
        object_start_poses = {object_asset_name: gymapi.Transform() for object_asset_name in object_assets.keys()}
        for object_asset_name in ["door_back", "room1", "room2"]:
            object_start_poses[object_asset_name].p = gymapi.Vec3(*cfg_asset["placement"][object_asset_name]["pos"])
            object_start_poses[object_asset_name].r = gymapi.Quat(*R.from_euler('xyz', cfg_asset["placement"][object_asset_name]["rot"]).as_quat())
        
        #* set camera sensor porps and pose
        self.camera_props = gymapi.CameraProperties()
        self.camera_props.width = 224
        self.camera_props.height = 224
        self.camera_props.enable_tensors = True
        self.camera_props.use_collision_geometry = False
        if self.repre_type in ['handcrafted','eureka']:
            self.camera = None
        if self.camera in [None]:
            self.camera_place_pos = None
            self.camera_lookat_pos = None
        elif self.camera == 'default':
            self.camera_place_pos = gymapi.Vec3(1.0, 0.0, 0.5)
            self.camera_lookat_pos = gymapi.Vec3(0.0, 0.0, 0.5)
        elif self.camera == 'down':
            self.camera_place_pos = gymapi.Vec3(0.8, 0, 0.4)
            self.camera_lookat_pos = gymapi.Vec3(0, 0, 0.5)
        elif self.camera == 'up':
            self.camera_place_pos = gymapi.Vec3(1.3, 0, 0.7)
            self.camera_lookat_pos = gymapi.Vec3(0.0, 0, 0.5)
        elif self.camera == 'demo':
            self.camera_props.width = 3840
            self.camera_props.height = 2160
            self.camera_place_pos = gymapi.Vec3(2.0, 0.0, 0.5)
            self.camera_lookat_pos = gymapi.Vec3(0.0, 0.0, 0.5)
        else:
            raise NotImplementedError(f"camera **{self.camera}** not implemented")

        #* construct environment
        self.envs = []
        self.robots = []
        self.robots_another = []
        self.robot_start_states = []
        self.robot_indices = []
        self.robot_another_indices = []
        self.objects_start_states = {k: [] for k in object_assets.keys()}
        self.objects_indices = {k: [] for k in object_assets.keys()}
        self.objects_handle = {k: [] for k in object_assets.keys()}
        self.camera_images = []
        self.attractor_handles = []
        self.attractor_another_handles = []
        self.dof_lower_limits = []
        self.dof_upper_limits = []
        for i in range(self.num_envs):
            # create env instance
            env_ptr = self.gym.create_env(self.sim, lower, upper, num_per_row)
        
            # set aggregate mode
            if self.aggregate_mode >= 1:
                self.gym.begin_aggregate(env_ptr, max_agg_bodies, max_agg_shapes, True)
            
            # add robot actor
            robot_actor = self.gym.create_actor(env_ptr, robot_asset, robot_start_pose, "robot", i, 0, 0)
            self.robot_start_states.append([robot_start_pose.p.x, robot_start_pose.p.y, robot_start_pose.p.z,
                                            robot_start_pose.r.x, robot_start_pose.r.y, robot_start_pose.r.z, robot_start_pose.r.w,
                                            0, 0, 0, 0, 0, 0])
            robot_another_actor = self.gym.create_actor(env_ptr, robot_another_asset, robot_another_start_pose, "another_robot", i, 0, 0)
            
            # get robot actor dof indices
            if i == 0:
                robot_dof_names = [self.gym.get_asset_dof_name(robot_asset, i) for i in range(self.num_robot_dofs)]
                robot_another_dof_names = [self.gym.get_asset_dof_name(robot_another_asset, i) for i in range(self.num_robot_dofs)]
                self.robot_dof_indices = [self.gym.find_actor_dof_index(env_ptr, robot_actor, name, gymapi.DOMAIN_ENV) for name in robot_dof_names]
                self.robot_dof_indices = to_torch(self.robot_dof_indices, dtype=torch.long, device=self.device)
                self.robot_another_dof_indices = [self.gym.find_actor_dof_index(env_ptr, robot_another_actor, name, gymapi.DOMAIN_ENV) for name in robot_another_dof_names]
                self.robot_another_dof_indices = to_torch(self.robot_another_dof_indices, dtype=torch.long, device=self.device)

            self.gym.set_actor_dof_properties(env_ptr, robot_actor, robot_dof_props)
            robot_idx = self.gym.get_actor_index(env_ptr, robot_actor, gymapi.DOMAIN_SIM)
            self.robot_indices.append(robot_idx)

            self.gym.set_actor_dof_properties(env_ptr, robot_another_actor, robot_another_dof_props)
            robot_another_idx = self.gym.get_actor_index(env_ptr, robot_another_actor, gymapi.DOMAIN_SIM)
            self.robot_another_indices.append(robot_another_idx)
            dof_states = self.gym.get_actor_dof_states(env_ptr, robot_actor, gymapi.STATE_ALL)
            for j in range(self.num_robot_dofs):
                #! for saving goal image | set robot dof
                # dof_states['pos'][j] = robot_goal_dof[j]
                dof_states['pos'][j] = robot_start_dof[j]
                dof_states['vel'][j] = 0.0
            self.gym.set_actor_dof_states(env_ptr, robot_actor, dof_states, gymapi.STATE_ALL)

            another_dof_states = self.gym.get_actor_dof_states(env_ptr, robot_another_actor, gymapi.STATE_ALL)
            for j in range(self.num_robot_dofs):
                #! for saving goal image | set robot dof
                # dof_states['pos'][j] = robot_goal_dof[j]
                another_dof_states['pos'][j] = robot_start_dof[j]
                another_dof_states['vel'][j] = 0.0
            self.gym.set_actor_dof_states(env_ptr, robot_another_actor, another_dof_states, gymapi.STATE_ALL)

            #* set attractor variables if use end-effector-control
            if self.act_type in ['end_effector_pose']:
                franka_body_dict = self.gym.get_actor_rigid_body_dict(env_ptr, robot_actor)
                franka_body_props = self.gym.get_actor_rigid_body_states(env_ptr, robot_actor, gymapi.STATE_POS)
                #! 'panda_hand' do not exist in franka_body_dict
                gripper_handle = self.gym.find_actor_rigid_body_handle(env_ptr, robot_actor, 'robot0:panda_hand')
                ## Initialize the attractor
                attractor_properties = gymapi.AttractorProperties()
                # attractor_properties.axes = gymapi.AXIS_TRANSLATION
                attractor_properties.axes = gymapi.AXIS_ALL
                attractor_properties.target = franka_body_props['pose'][:][franka_body_dict['robot0:panda_hand']]
                attractor_properties.rigid_handle = gripper_handle
                pose = attractor_properties.target
                self.end_effector_default_pose = to_torch([pose.p.x, pose.p.y, pose.p.z,
                                                      pose.r.x, pose.r.y, pose.r.z, pose.r.w], dtype=torch.float, device=self.device)
                attractor_handle = self.gym.create_rigid_body_attractor(env_ptr, attractor_properties)
                self.attractor_handles.append(attractor_handle)
                self.franka_leftfinger_handle = self.gym.find_actor_rigid_body_handle(env_ptr, robot_actor, 'robot0:panda_leftfinger')
                self.franka_rightfinger_handle = self.gym.find_actor_rigid_body_handle(env_ptr, robot_actor, 'robot1:panda_rightfinger')

                another_franka_body_dict = self.gym.get_actor_rigid_body_dict(env_ptr, robot_another_actor)
                another_franka_body_props = self.gym.get_actor_rigid_body_states(env_ptr, robot_another_actor, gymapi.STATE_POS)
                another_gripper_handle = self.gym.find_actor_rigid_body_handle(env_ptr, robot_another_actor, 'robot1:panda_hand')
                another_attractor_properties = gymapi.AttractorProperties()
                another_attractor_properties.axes = gymapi.AXIS_ALL
                another_attractor_properties.target = another_franka_body_props['pose'][:][another_franka_body_dict['robot1:panda_hand']]
                another_attractor_properties.rigid_handle = another_gripper_handle
                another_pose = another_attractor_properties.target
                self.another_end_effector_default_pose = to_torch([another_pose.p.x, another_pose.p.y, another_pose.p.z,
                                                      another_pose.r.x, another_pose.r.y, another_pose.r.z, another_pose.r.w], dtype=torch.float, device=self.device)
                another_attractor_handle = self.gym.create_rigid_body_attractor(env_ptr, another_attractor_properties)
                self.attractor_another_handles.append(another_attractor_handle)
                self.another_franka_leftfinger_handle = self.gym.find_actor_rigid_body_handle(env_ptr, robot_another_actor, 'robot0:panda_leftfinger')
                self.another_franka_rightfinger_handle = self.gym.find_actor_rigid_body_handle(env_ptr, robot_another_actor, 'robot1:panda_rightfinger')

            elif self.act_type in ['dummy_interaction_sphere']:
                self.sphere_handle = self.gym.find_actor_rigid_body_handle(env_ptr, robot_actor, 'robot0:sphere_link')
                self.another_sphere_handle = self.gym.find_actor_rigid_body_handle(env_ptr, robot_another_actor, 'robot1:sphere_link')
            
            if i == 0:
                dof_props = self.gym.get_actor_dof_properties(env_ptr, robot_actor)
                for i_dof in range(dof_props['lower'].shape[0]):
                    self.dof_lower_limits.append(dof_props['lower'][i_dof])
                    self.dof_upper_limits.append(dof_props['upper'][i_dof])
                    assert dof_props['hasLimits'][i_dof] == True
                if self.obs_type == "full_state":
                    for i_dof in range(dof_props['lower'].shape[0]):
                        self.dof_lower_limits.append(dof_props['lower'][i_dof])
                        self.dof_upper_limits.append(dof_props['upper'][i_dof])
                    assert dof_props['hasLimits'][i_dof] == True

                    
            # add manipulated object
            for object_asset_name in object_assets.keys():
                object_asset = object_assets[object_asset_name]
                object_start_pose = object_start_poses[object_asset_name]
                self.objects_start_states[object_asset_name].append([object_start_pose.p.x, object_start_pose.p.y, object_start_pose.p.z,
                                                                     object_start_pose.r.x, object_start_pose.r.y, object_start_pose.r.z, object_start_pose.r.w,
                                                                     0, 0, 0, 0, 0, 0])
                
                # set object dof properties
                object_dof_props = self.gym.get_asset_dof_properties(object_asset)
                for object_dof_prop in object_dof_props:
                    object_dof_prop[4] = 100
                    object_dof_prop[5] = 100
                    object_dof_prop[6] = 0
                    object_dof_prop[7] = 1
                    object_dof_prop[8] = 0.05

                object_handle = self.gym.create_actor(env_ptr, object_asset, object_start_pose, object_asset_name, i, 0, 0)
                self.gym.set_actor_dof_properties(env_ptr, object_handle, object_dof_props)
                object_idx = self.gym.get_actor_index(env_ptr, object_handle, gymapi.DOMAIN_SIM)
                self.objects_indices[object_asset_name].append(object_idx)
                self.objects_handle[object_asset_name] = object_handle
                
                # set friction
                object_shape_props = self.gym.get_actor_rigid_shape_properties(env_ptr, object_handle)
                for object_shape_prop in object_shape_props:
                    object_shape_prop.friction = 1.0
                self.gym.set_actor_rigid_shape_properties(env_ptr, object_handle, object_shape_props)

                if i == 0:
                    dof_props = self.gym.get_actor_dof_properties(env_ptr, object_handle)
                    for i_dof in range(dof_props['lower'].shape[0]):
                        self.dof_lower_limits.append(dof_props['lower'][i_dof])
                        self.dof_upper_limits.append(dof_props['upper'][i_dof])
                        assert dof_props['hasLimits'][i_dof] == True
                
                if object_asset_name == 'door_back':
                    self.door_left_dof_handle = self.gym.find_actor_dof_handle(env_ptr, object_handle, 'joint_1')
                    self.door_right_dof_handle = self.gym.find_actor_dof_handle(env_ptr, object_handle, 'joint_2')
                if object_asset_name == 'door_back':
                    dof_states = self.gym.get_actor_dof_states(env_ptr, object_handle, gymapi.STATE_ALL)
                    dof_states['pos'][0] = 1.57
                    dof_states['vel'][0] = 0.0
                    dof_states['pos'][1] = 1.57
                    dof_states['vel'][1] = 0.0
                    self.gym.set_actor_dof_states(env_ptr, object_handle, dof_states, gymapi.STATE_ALL)

            if i == 0:
                self.dof_lower_limits = to_torch(self.dof_lower_limits, device=self.device)
                self.dof_upper_limits = to_torch(self.dof_upper_limits, device=self.device)

            #* set camera in env
            if self.camera is not None:
                camera_handle = self.gym.create_camera_sensor(env_ptr, self.camera_props)
                self.gym.set_camera_location(camera_handle, env_ptr, self.camera_place_pos, self.camera_lookat_pos)
                self.camera_handle = camera_handle
                self.view_m = torch.tensor(self.gym.get_camera_view_matrix(self.sim, env_ptr, camera_handle)).to(self.device)
                self.projection_m = torch.tensor(self.gym.get_camera_proj_matrix(self.sim, env_ptr, camera_handle)).to(self.device)
                self.camera_w = self.camera_props.width
                camera_tensor = self.gym.get_camera_image_gpu_tensor(self.sim, env_ptr, camera_handle, gymapi.IMAGE_COLOR)
                # camera_tensor = gymtorch.wrap_tensor(camera_tensor)
                self.camera_images.append(gymtorch.wrap_tensor(camera_tensor))  #note: dtype is torch.unit8

            if self.aggregate_mode > 0:
                self.gym.end_aggregate(env_ptr)
            
            self.envs.append(env_ptr)
            self.robots.append(robot_actor)
            self.robots_another.append(robot_another_actor)
        #* end of environment construction

        # list to torch
        self.robot_start_states = torch.tensor(self.robot_start_states, dtype=torch.float, device=self.device).view(self.num_envs, 13)
        self.objects_start_states = {k: torch.tensor(v, dtype=torch.float, device=self.device).view(self.num_envs, 13) for k, v in self.objects_start_states.items()}
        self.robot_indices = torch.tensor(self.robot_indices, dtype=torch.long, device=self.device)
        self.robot_another_indices = torch.tensor(self.robot_another_indices, dtype=torch.long, device=self.device)
        self.objects_indices = {k: torch.tensor(v, dtype=torch.long, device=self.device) for k, v in self.objects_indices.items()}
        self.objects_handle = {k: torch.tensor(v, dtype=torch.long, device=self.device) for k, v in self.objects_handle.items()}
        self.num_rigid_bodies = self.num_robot_bodies * 2 + self.num_object_bodies

    def compute_reward(self, actions):
        # support hand-crafted reward
        if self.repre_type in ["r3m", "ag2x2", "vip", ]:
            self.gym.render_all_camera_sensors(self.sim)
            self.gym.start_access_image_tensors(self.sim)
            camera_images = []
            
            for i in range(self.num_envs):
                camera_images.append(self.camera_images[i].clone()[..., :3])
            grippers_pos = torch.cat((torch.tensor(self.gripper_pos).unsqueeze(1), torch.tensor(self.gripper_another_pos).unsqueeze(1)), dim=1)  # [B, 2 ,3]
            hands = world2screen(grippers_pos, self.view_m, self.projection_m, self.camera_w)
            self.gym.end_access_image_tensors(self.sim)
            camera_images = torch.stack(camera_images, dim=0)
            camera_images = camera_images.float() / 255.
            value, _ = self.repre_model(camera_images, hands)

            #* reward shaping for different representation model
            if self.repre_type in ['r3m', 'ag2x2', 'vip']:
                if self.initial_value is None:
                    self.initial_value = value.clone().mean().cpu().item()
                reward = (1 / self.initial_value) * (self.initial_value - value)
            else:
                raise NotImplementedError

            if self.cfg["env"]["rewardType"] == 'plain':
                reward = 3 - value
            elif self.cfg["env"]["rewardType"] == 'efficiency':
                reward = torch.where(reward < 0, torch.exp(reward) - 1, 10 * (torch.exp(2 * reward) - 1))
            else:
                raise NotImplementedError
        elif self.repre_type in ["handcrafted"]:
            door_left_dof_pos = self.door_left_dof_pos.clone()
            door_right_dof_pos = self.door_right_dof_pos.clone()
            reward = (3.14-door_right_dof_pos-door_left_dof_pos) / 3.14
            reward = torch.where(reward < 0, torch.exp(reward) - 1, 10 * (torch.exp(2 * reward) - 1))
        elif self.repre_type in ['eureka']:
            if self.eureka_seed == 0:
                rewards_dict = {}
                temp_door_right = torch.tensor(0.1)
                temp_door_left = torch.tensor(0.1)
                rewards_dict['door_right_inward_position'] = -torch.exp((self.door_right_dof_pos - 0) / temp_door_right)
                rewards_dict['door_left_inward_position'] = -torch.exp((self.door_left_dof_pos - 0) / temp_door_left)
                reward = torch.sum(torch.stack(list(rewards_dict.values())), dim=0)
            elif self.eureka_seed == 1:
                temp_right = torch.tensor(0.1, device=self.door_right_dof_pos.device)
                temp_left = torch.tensor(0.1, device=self.door_left_dof_pos.device)
                reward_right = 1.0 - torch.exp(-(self.door_right_dof_pos / temp_right))
                reward_left = 1.0 - torch.exp(-(self.door_left_dof_pos / temp_left))
                reward = torch.mean(torch.stack((reward_right, reward_left)), dim=0)
            elif self.eureka_seed == 2:
                temp_open = torch.tensor(0.01)
                reward = torch.where( 
                    self.door_right_dof_pos <= 0,
                    torch.ones_like(self.door_right_dof_pos),
                    torch.exp(-temp_open * (self.door_right_dof_pos - 0))  
                )
            else:
                raise NotImplementedError
        else:
            raise NotImplementedError
        
        goal_door_dof_pos = 0
        door_right_dof_pos = self.door_right_dof_pos.clone()
        door_left_dof_pos = self.door_left_dof_pos.clone()
        achived_goal = (torch.abs(goal_door_dof_pos - door_right_dof_pos) < 0.2) & (torch.abs(goal_door_dof_pos - door_left_dof_pos) < 0.2)
        self.rew_buf = reward.clone()
        self.successes = torch.where(self.successes == 0,   
                                torch.where(achived_goal, torch.ones_like(self.successes), self.successes), self.successes)
        #self.scores = torch.min(door_right_dof_pos / goal_door_dof_pos, door_left_dof_pos / goal_door_dof_pos)
        self.scores = 0.5*((1.57-door_right_dof_pos) / 1.57+ (1.57-door_left_dof_pos) / 1.57)
        self.scores = torch.where(self.scores > 1.0, torch.ones_like(self.scores), self.scores)
        self.scores = torch.where(self.scores < 0.0, torch.zeros_like(self.scores), self.scores)

        # self.reset_buf = torch.where(self.successes > 0, torch.ones_like(self.reset_buf), self.reset_buf)
        self.reset_buf = torch.where(self.progress_buf >= self.max_episode_length, torch.ones_like(self.reset_buf), self.reset_buf)
        self.reset_goal_buf = torch.zeros_like(self.reset_buf)
        self.consecutive_successes = torch.where(self.reset_buf > 0, self.successes * self.reset_buf, self.consecutive_successes)
        self.success_scores = torch.where(self.reset_buf > 0, self.scores * self.reset_buf, self.success_scores)

        # self.extras['successes'] = self.successes
        self.extras['consecutive_successes'] = self.consecutive_successes.mean().unsqueeze(0)
        self.extras['success_scores'] = self.success_scores.mean().unsqueeze(0)
        self.extras['indicator_right'] = self.door_right_dof_pos.clone().mean().unsqueeze(0)
        self.extras['indicator_left'] = self.door_left_dof_pos.clone().mean().unsqueeze(0)

        if self.consecutive_successes.mean() > self.extras['max_consecutive_successes']:
            self.extras['max_consecutive_successes'] = self.consecutive_successes.mean().unsqueeze(0)
        if self.success_scores.mean() > self.extras['max_success_scores']:
            self.extras['max_success_scores'] = self.success_scores.mean().unsqueeze(0)

    def compute_observations(self):
        """
        Compute the observations of all environment. The core function is ...
        which we will introduce in detail there
        """
        self.gym.refresh_dof_state_tensor(self.sim)
        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_rigid_body_state_tensor(self.sim)
        self.gym.refresh_force_sensor_tensor(self.sim)
        self.gym.refresh_dof_force_tensor(self.sim)

        self.dof_states = gymtorch.wrap_tensor(self.gym.acquire_dof_state_tensor(self.sim)).view(self.num_envs, -1, 2)
        self.robot_dof_state = self.dof_states[:, :self.num_robot_dofs, :]
        self.robot_dof_pos = self.robot_dof_state[..., 0]
        self.robot_dof_vel = self.robot_dof_state[..., 1]
        self.robot_another_dof_state = self.dof_states[:, self.num_robot_dofs:2*self.num_robot_dofs, :]
        self.robot_another_dof_pos = self.robot_another_dof_state[..., 0]
        self.robot_another_dof_vel = self.robot_another_dof_state[..., 1]
        self.door_right_dof_pos = self.dof_states[:, self.door_right_dof_handle, 0]
        self.door_right_dof_vel = self.dof_states[:, self.door_right_dof_handle, 1]
        self.door_left_dof_pos = self.dof_states[:, self.door_left_dof_handle, 0]
        self.door_left_dof_vel = self.dof_states[:, self.door_left_dof_handle, 1]
        self.rigid_body_states = gymtorch.wrap_tensor(self.gym.acquire_rigid_body_state_tensor(self.sim)).view(self.num_envs, -1, 13)
        np_rigid_body_states = self.rigid_body_states.cpu().numpy()
        np_attached_body_handle = self.attached_body_handle.cpu().numpy()
        if self.act_type in ['dummy_interaction_sphere']:
            self.gripper_pose = np_rigid_body_states[:, self.sphere_handle, 0:7]
            self.gripper_another_pose = np_rigid_body_states[:, self.another_sphere_handle, 0:7]
            self.gripper_pos = self.gripper_pose[:, 0:3]
            self.gripper_rot = self.gripper_pose[:, 3:7]
            self.gripper_another_pos = self.gripper_another_pose[:, 0:3]
            self.gripper_another_rot = self.gripper_another_pose[:, 3:7]
            for i in range(self.num_envs):
                if self.attached_body_handle[i] != -1:
                    body_trans_t = np_rigid_body_states[i, self.attached_body_handle[i], :3]
                    body_rot_t = np_rigid_body_states[i, self.attached_body_handle[i], 3:7]
                    body_rot_t = R.from_quat(body_rot_t).as_matrix()
                    body_tm_t = np.eye(4)
                    body_tm_t[:3, :3] = body_rot_t
                    body_tm_t[:3, 3] = body_trans_t
                    
                    body_tm = self.body_tms[self.attached_info_indices[i]]
                    body_tm_t = body_tm_t @ np.linalg.inv(body_tm)
                    attach_tm = body_tm_t @ self.related_tms[self.attached_info_indices[i]] @ body_tm
                    attach_trans = attach_tm[:3, 3]
                    attach_rot = R.from_matrix(attach_tm[:3, :3]).as_quat()
                    self.gripper_pos[i] = attach_trans
                    self.gripper_rot[i] = attach_rot

                if self.another_attached_body_handle[i] != -1:
                    body_trans_t = np_rigid_body_states[i, self.another_attached_body_handle[i], :3]
                    body_rot_t = np_rigid_body_states[i, self.another_attached_body_handle[i], 3:7]
                    body_rot_t = R.from_quat(body_rot_t).as_matrix()
                    body_tm_t = np.eye(4)
                    body_tm_t[:3, :3] = body_rot_t
                    body_tm_t[:3, 3] = body_trans_t
                    
                    body_tm = self.body_tms[self.another_attached_info_indices[i]]
                    body_tm_t = body_tm_t @ np.linalg.inv(body_tm)
                    attach_tm = body_tm_t @ self.related_tms[self.another_attached_info_indices[i]] @ body_tm
                    attach_trans = attach_tm[:3, 3]
                    attach_rot = R.from_matrix(attach_tm[:3, :3]).as_quat()
                    self.gripper_another_pos[i] = attach_trans
                    self.gripper_another_rot[i] = attach_rot
            self.gripper_pos = torch.tensor(self.gripper_pos, device=self.device)
            self.gripper_another_pos = torch.tensor(self.gripper_another_pos, device=self.device)
            self.gripper_rot = torch.tensor(self.gripper_rot, device=self.device)
            self.gripper_another_rot = torch.tensor(self.gripper_another_rot, device=self.device)
            if self.num_envs == 1:
                self.gripper_pos_history.append(self.gripper_pos.squeeze(0).cpu().numpy())
                self.another_gripper_pos_history.append(self.gripper_another_pos.squeeze(0).cpu().numpy())
                self.update_smo_metric()
        elif self.act_type in ['end_effector_pose', 'robot_joint_pose']:
            self.gripper_pos = torch.zeros((self.num_envs, 3), device=self.device)
            self.gripper_another_pos = torch.zeros((self.num_envs, 3), device=self.device)
        else:
            raise NotImplementedError
        if not self.is_planning:
            if self.obs_type == "full_state":
                self.compute_full_state()
            else:
                raise NotImplementedError(f"The observation type {self.obs_type} is not implemented yet")

    def compute_full_state(self):
        self.obs_buf[:, :self.num_dofs] = self.dof_states[:, :, 0].clone().detach()
        tmp_vel = self.dof_states[:, :, 1].clone().detach()
        self.obs_buf[:, self.num_dofs : 2*self.num_dofs] = self.vel_obs_scale * tmp_vel
        self.obs_buf[:, 2*self.num_dofs] = self.progress_buf.clone()
        if self.act_type in ['dummy_interaction_sphere']:
            self.obs_buf[:, 2*self.num_dofs + 1] = self.is_attached.float()
            self.obs_buf[:, 2*self.num_dofs + 2] = self.another_is_attached.float()

    def reset_target_pose(self, env_ids, apply_reset=False):
        self.reset_goal_buf[env_ids] = 0
    
    def reset(self, env_ids, goal_env_ids):
        """
        Reset and randomize the environment

        Args:
            env_ids (tensor): The index of the environment that needs to reset

            goal_env_ids (tensor): The index of the environment that only goals need reset

        """
        if self.randomize:
            # assert False, "The randomization is not used in such this task"
            # self.apply_randomizations(self.randomization_params)
            randomize_pos = torch.randn_like(self.robot_dof_default_pos)
            randomize_pos[:3] *= 0.05
            randomize_pos[:3] = torch.clip(randomize_pos[:3], -0.1, 0.1)
            randomize_pos[3:6] *= 0.5
            randomize_pos[6:] *= 0.
            robot_dof_reset_pos = self.robot_dof_default_pos + randomize_pos
        else:
            robot_dof_reset_pos = self.robot_dof_default_pos
        
        self.reset_target_pose(env_ids)
        
        # reset the robot dof
        self.robot_dof_pos[env_ids, :] = robot_dof_reset_pos
        self.robot_dof_vel[env_ids, :] = self.robot_dof_default_vel
        self.robot_another_dof_pos[env_ids, :] = robot_dof_reset_pos
        self.robot_another_dof_vel[env_ids, :] = self.robot_dof_default_vel

        self.prev_targets[env_ids, :self.num_robot_dofs] = robot_dof_reset_pos
        self.cur_targets[env_ids, :self.num_robot_dofs] = self.robot_dof_default_pos
        self.prev_targets[env_ids, self.num_robot_dofs:2*self.num_robot_dofs] = robot_dof_reset_pos
        self.cur_targets[env_ids, self.num_robot_dofs:2*self.num_robot_dofs] = self.robot_dof_default_pos
        
        if self.act_type in ['end_effector_pose']:
            self.attractor_prev_targets[env_ids, :] = self.end_effector_default_pose
            self.attractor_cur_targets[env_ids, :] = self.end_effector_default_pose
            self.attractor_another_prev_targets[env_ids, :] = self.another_end_effector_default_pose
            self.attractor_another_cur_targets[env_ids, :] = self.another_end_effector_default_pose

        # reset the object dof
        self.dof_states[env_ids, :] = self.dof_start_state_tensor[env_ids, :]
        self.dof_states[env_ids, :self.num_robot_dofs, 0] = robot_dof_reset_pos
        self.dof_states[env_ids, :self.num_robot_dofs, 1] = self.robot_dof_default_vel
        self.dof_states[env_ids, self.num_robot_dofs:2*self.num_robot_dofs, 0] = robot_dof_reset_pos
        self.dof_states[env_ids, self.num_robot_dofs:2*self.num_robot_dofs, 1] = self.robot_dof_default_vel

        # reset objects root state
        self.root_state_tensor[env_ids, :] = self.root_start_state_tensor[env_ids, :]

        #* gym reset
        self.gym.set_dof_state_tensor(self.sim, gymtorch.unwrap_tensor(self.dof_states))
        self.gym.set_dof_position_target_tensor(self.sim, gymtorch.unwrap_tensor(self.cur_targets))
        self.gym.set_actor_root_state_tensor(self.sim, gymtorch.unwrap_tensor(self.root_start_state_tensor))

        self.progress_buf[env_ids] = 0
        self.reset_buf[env_ids] = 0
        self.successes[env_ids] = 0

        #* reset the control-stage
        self._set_control_stage(env_ids, False, -1, False, -1)

    def _set_control_stage(self, env_ids, is_attached, attached_body_index, another_is_attached, another_attached_body_index):
        self.is_attached[env_ids] = is_attached
        self.attached_body_handle[env_ids] = attached_body_index
        self.another_is_attached[env_ids] = another_is_attached
        self.another_attached_body_handle[env_ids] = another_attached_body_index
    
    def pre_physics_step(self, actions):
        """
        The pre-processing of the physics step. Determine whether the reset environment is needed, 
        and calculate the next movement of Shadowhand through the given action. The n-dimensional 
        action space as shown in below:
        
        Index    Description
        0 - n    Joint angles of franka robot
        n+1 - 2n Joint angles of another franka robot
        Args:
            actions (tensor): Actions of agents in the all environment 
        """
        env_ids = self.reset_buf.nonzero(as_tuple=False).squeeze(-1)
        goal_env_ids = self.reset_goal_buf.nonzero(as_tuple=False).squeeze(-1)
        #* We need to reset the whole environment
        # if only goals need reset, then call set API
        if len(goal_env_ids) > 0 and len(env_ids) == 0:
            self.reset_target_pose(goal_env_ids, apply_reset=True)
        # if goals need reset in addition to other envs, call set API in reset()
        elif len(goal_env_ids) > 0:
            self.reset_target_pose(goal_env_ids)

        if len(env_ids) > 0:
            self.reset(env_ids, goal_env_ids)

        if self.act_type == 'dummy_interaction_sphere':
            self.actions = actions.clone().to(self.device)[:, 0:3]
            self.forces = actions.clone().to(self.device)[:, 3:6]
            self.forces_magnitude = actions.clone().to(self.device)[:, 6:7]
            self.forces_magnitude = 1 / (1 + torch.exp(-self.forces_magnitude))
            self.another_actions = actions.clone().to(self.device)[:, 7:10]
            self.another_forces = actions.clone().to(self.device)[:, 10:13]
            self.another_forces_magnitude = actions.clone().to(self.device)[:, 13:14]
            self.another_forces_magnitude = 1 / (1 + torch.exp(-self.another_forces_magnitude))
        elif self.act_type == 'robot_joint_pose':
            num = actions.size(1)
            self.actions = actions[:, 0:(num//2)].clone().to(self.device)
            self.another_actions = actions[:, (num//2):].clone().to(self.device)

        if self.act_type in ['robot_joint_pose']:
            if self.use_relative_control:
                targets = self.prev_targets[:, self.robot_dof_indices] + self.robot_dof_speed_scale * self.dt * self.actions
                another_targets = self.prev_targets[:, self.robot_another_dof_indices] + self.robot_dof_speed_scale * self.dt * self.another_actions
                self.cur_targets[:, self.robot_dof_indices] = tensor_clamp(targets,
                                                                    self.robot_dof_lower_limits[self.robot_dof_indices], self.robot_dof_upper_limits[self.robot_dof_indices])
                self.cur_targets[:, self.robot_another_dof_indices] = tensor_clamp(another_targets,
                                                                    self.robot_dof_lower_limits[self.robot_dof_indices], self.robot_dof_upper_limits[self.robot_dof_indices])
            else:
                raise NotImplementedError("The absolute control is not implemented yet")
            self.prev_targets[:, self.robot_dof_indices] = self.cur_targets[:, self.robot_dof_indices]
            self.prev_targets[:, self.robot_another_dof_indices] = self.cur_targets[:, self.robot_another_dof_indices]
            self.gym.set_dof_position_target_tensor(self.sim, gymtorch.unwrap_tensor(self.cur_targets))
        
        elif self.act_type in ['end_effector_pose']:
            assert False, "abort the end_effector_pose mode"
        elif self.act_type in ['dummy_interaction_sphere']:
            if self.use_relative_control:
                targets = self.prev_targets[:, self.robot_dof_indices] + self.robot_dof_speed_scale * self.dt * self.actions
                self.cur_targets[:, self.robot_dof_indices] = tensor_clamp(targets,
                                                                    self.robot_dof_lower_limits[self.robot_dof_indices], self.robot_dof_upper_limits[self.robot_dof_indices])
                another_targets = self.prev_targets[:, self.robot_another_dof_indices] + self.robot_dof_speed_scale * self.dt * self.another_actions
                self.cur_targets[:, self.robot_another_dof_indices] = tensor_clamp(another_targets,
                                                                    self.robot_dof_lower_limits[self.robot_dof_indices], self.robot_dof_upper_limits[self.robot_dof_indices])
            else:
                assert False, 'not implemented yet'
            self.prev_targets[:, self.robot_dof_indices] = self.cur_targets[:, self.robot_dof_indices]
            self.prev_targets[:, self.robot_another_dof_indices] = self.cur_targets[:, self.robot_another_dof_indices]
            
            position_control_envs = (self.is_attached == False).nonzero().reshape(-1)
            force_control_envs = (self.is_attached == True).nonzero().reshape(-1)
            another_position_control_envs = (self.another_is_attached == False).nonzero().reshape(-1)
            another_force_control_envs = (self.another_is_attached == True).nonzero().reshape(-1)
            #* apply position on robot (asynchoronous)
            position_robot_indices = torch.unique(self.robot_indices[position_control_envs]).to(torch.int32)
            force_robot_indices = torch.unique(self.robot_indices[force_control_envs]).to(torch.int32)
            another_position_robot_indices = torch.unique(self.robot_another_indices[another_position_control_envs]).to(torch.int32)
            another_force_robot_indices = torch.unique(self.robot_another_indices[another_force_control_envs]).to(torch.int32)
            
            if len(position_robot_indices) > 0 and len(another_position_robot_indices) > 0:
                position_indices = torch.cat((position_robot_indices, another_position_robot_indices))
                self.gym.set_dof_position_target_tensor_indexed(self.sim,
                                                                gymtorch.unwrap_tensor(self.cur_targets),
                                                                gymtorch.unwrap_tensor(position_indices), len(position_indices))
            elif len(position_robot_indices) > 0 and len(another_position_robot_indices) <= 0:
                self.gym.set_dof_position_target_tensor_indexed(self.sim,
                                                                gymtorch.unwrap_tensor(self.cur_targets),
                                                                gymtorch.unwrap_tensor(position_robot_indices), len(position_robot_indices))
            elif len(another_position_robot_indices) > 0 and len(position_robot_indices) <= 0:
                self.gym.set_dof_position_target_tensor_indexed(self.sim,
                                                                gymtorch.unwrap_tensor(self.cur_targets),
                                                                gymtorch.unwrap_tensor(another_position_robot_indices), len(another_position_robot_indices))
            dof_states = torch.zeros_like(self.dof_states)
            dof_states[:, self.robot_dof_indices[[2]], 0] = 3.0  # 2
            dof_states[:, self.robot_another_dof_indices[[2]], 0] = 3.0  # 5
            dof_states[:, self.robot_dof_indices[[0]], 0] = 3.0  # 2
            dof_states[:, self.robot_another_dof_indices[[0]], 0] = 3.0  # 5
            if len(force_robot_indices) > 0 and len(another_force_robot_indices) > 0:
                force_indices = torch.cat((force_robot_indices, another_force_robot_indices))
                self.gym.set_dof_state_tensor_indexed(self.sim,
                                                    gymtorch.unwrap_tensor(dof_states),
                                                    gymtorch.unwrap_tensor(force_indices), len(force_indices))
            elif len(force_robot_indices) > 0 and len(another_force_robot_indices) <= 0:
                self.gym.set_dof_state_tensor_indexed(self.sim,
                                                    gymtorch.unwrap_tensor(dof_states),
                                                    gymtorch.unwrap_tensor(force_robot_indices), len(force_robot_indices))
            elif len(another_force_robot_indices) > 0 and len(force_robot_indices) <= 0:
                self.gym.set_dof_state_tensor_indexed(self.sim,
                                                    gymtorch.unwrap_tensor(dof_states),
                                                    gymtorch.unwrap_tensor(another_force_robot_indices), len(another_force_robot_indices))
            #* apply force on objects
            GLOBAL_FORCES_SCALE = 500.
            dummy_forces = torch.nn.functional.normalize(self.forces, dim=-1)
            dummy_forces = GLOBAL_FORCES_SCALE * self.forces_magnitude * dummy_forces
            rigid_body_forces = torch.zeros((self.num_envs, self.num_rigid_bodies, 3), dtype=torch.float, device=self.device)
            for env_idx in force_control_envs:
                rigid_body_forces[env_idx, self.attached_body_handle[env_idx], :] += dummy_forces[env_idx, :]
            another_dummy_forces = torch.nn.functional.normalize(self.another_forces, dim=-1)
            another_dummy_forces = GLOBAL_FORCES_SCALE * self.another_forces_magnitude * another_dummy_forces
            for env_idx in another_force_control_envs:
                rigid_body_forces[env_idx, self.another_attached_body_handle[env_idx], :] += another_dummy_forces[env_idx, :]
            self.gym.apply_rigid_body_force_tensors(self.sim, gymtorch.unwrap_tensor(rigid_body_forces), None, gymapi.ENV_SPACE)

    def post_physics_step(self):
        self.randomize_buf += 1

        self.compute_observations()
        self.compute_reward(self.actions)
        ##* Change control stage if condition
        if self.act_type in ['dummy_interaction_sphere']:
            self._refresh_attached_states()
        self.progress_buf += 1
        if self.progress_buf[0] >= self.max_episode_length: 
            print(self.get_avg_smo())

    def _setup_attachable_body(self, ):
        self.is_attached = torch.zeros(self.num_envs, dtype=torch.bool, device=self.device)
        self.attached_body_handle = - torch.ones(self.num_envs, dtype=torch.int32, device=self.device)
        self.attached_info_indices = - torch.ones(self.num_envs, dtype=torch.int32, device=self.device)

        self.another_is_attached = torch.zeros(self.num_envs, dtype=torch.bool, device=self.device)
        self.another_attached_body_handle = - torch.ones(self.num_envs, dtype=torch.int32, device=self.device)
        self.another_attached_info_indices = - torch.ones(self.num_envs, dtype=torch.int32, device=self.device)

        self._attachable_points = []
        self._attachable_handles = []
        cfg_asset = self.cfg["env"]["asset"]
        env_asset_root = cfg_asset["object"]["assetRoot"]
        env_asset_place = cfg_asset["placement"]
        self.objects_attach_info = []
        self.att_tms = []
        self.body_tms = []
        self.related_tms = []
        object_name = 'door_back'
        object_attachs = pickle.load(open(os.path.join(env_asset_root, 'attachable', 'door_back1.pkl'), 'rb'))['data']
        object_trans = np.array(env_asset_place[object_name]['pos'])
        object_rot = np.array(env_asset_place[object_name]['rot'])
        object_rot = R.from_euler('xyz', object_rot).as_matrix()
        camt = compute_camera_transform(torch.tensor([0.2, 2.0, 0.0]), torch.tensor([0.0, 0.0, 0.0]))
        for att in object_attachs:
            att_point = att['translation']
            pos_new = cam2world(torch.tensor(att['translation']), torch.tensor(att['rotation_matrix']), camt)
            att_point = pos_new[:3, 3]
            att_point *= -1.0  #till here, attach pose relative to object
            att['translation'] = np.array(att_point)
            att['rotation_matrix'] = np.array(pos_new[:3, :3])  # update from pickle file to real relative pose
            att_point = object_rot @ np.array(att_point) + object_trans
            att_rot = object_rot @ (att['rotation_matrix'] @ R.from_euler('xyz', [0., 0., 1.57]).as_matrix())  # till here, attach pose in world
            att_tm = np.eye(4)
            att_tm[:3, :3] = att_rot
            att_tm[:3, 3] = att_point
            self.att_tms.append(att_tm)
            att_rot = R.from_matrix(att_rot).as_quat()
            self._attachable_points.append(att_point)
            att_rb_handle = self.gym.find_actor_rigid_body_handle(self.envs[0], self.objects_handle[object_name], 'link_2')  # right
            self._attachable_handles.append(att_rb_handle)
            body_tm = np.eye(4)  # initial attached rigid part pose matrix in world
            body_tm[:3, 3] = self.rigid_body_states[0, att_rb_handle, :3].cpu().numpy()
            body_tm[:3, :3] = R.from_quat(self.rigid_body_states[0, att_rb_handle, 3:7].cpu().numpy()).as_matrix()
            self.body_tms.append(body_tm)
            self.related_tms.append(att_tm @ np.linalg.inv(body_tm))
            self.objects_attach_info.append({
                'object_name': object_name,
                'attach_info': att,
                'object_rot': object_rot,
                'object_trans': object_trans,
            })
        object_attachs = pickle.load(open(os.path.join(env_asset_root, 'attachable', 'door_back2.pkl'), 'rb'))['data']
        object_trans = np.array(env_asset_place[object_name]['pos'])
        object_rot = np.array(env_asset_place[object_name]['rot'])
        object_rot = R.from_euler('xyz', object_rot).as_matrix()
        camt = compute_camera_transform(torch.tensor([0.2, -2.0, 0.0]), torch.tensor([0.0, 0.0, 0.0]))
        for att in object_attachs:
            att_point = att['translation']
            pos_new = cam2world(torch.tensor(att['translation']), torch.tensor(att['rotation_matrix']), camt)
            att_point = pos_new[:3, 3]
            att_point *= -1.0  #till here, attach pose relative to object
            att['translation'] = np.array(att_point)
            att['rotation_matrix'] = np.array(pos_new[:3, :3])  # update from pickle file to real relative pose
            att_point = object_rot @ np.array(att_point) + object_trans
            if att_point[2] > 0.7 or att_point[2] < 0.25:
                continue
            att_rot = object_rot @ (att['rotation_matrix'] @ R.from_euler('xyz', [0., 0., 1.57]).as_matrix())  # till here, attach pose in world
            att_tm = np.eye(4)
            att_tm[:3, :3] = att_rot
            att_tm[:3, 3] = att_point
            self.att_tms.append(att_tm)
            att_rot = R.from_matrix(att_rot).as_quat()
            self._attachable_points.append(att_point)
            att_rb_handle = self.gym.find_actor_rigid_body_handle(self.envs[0], self.objects_handle[object_name], 'link_1')  # left
            self._attachable_handles.append(att_rb_handle)
            body_tm = np.eye(4)  # initial attached rigid part pose matrix in world
            body_tm[:3, 3] = self.rigid_body_states[0, att_rb_handle, :3].cpu().numpy()
            body_tm[:3, :3] = R.from_quat(self.rigid_body_states[0, att_rb_handle, 3:7].cpu().numpy()).as_matrix()
            self.body_tms.append(body_tm)
            self.related_tms.append(att_tm @ np.linalg.inv(body_tm))
            self.objects_attach_info.append({
                'object_name': object_name,
                'attach_info': att,
                'object_rot': object_rot,
                'object_trans': object_trans,
            })
        self._attachable_points = np.stack(self._attachable_points, axis=0)
        self._attachable_points = torch.tensor(self._attachable_points, dtype=torch.float, device=self.device)
        self._attachable_handles = torch.tensor(self._attachable_handles, dtype=torch.int, device=self.device)
        self.att_tms = np.stack(self.att_tms, axis=0)
        self.body_tms = np.stack(self.body_tms, axis=0)
        self.related_tms = np.stack(self.related_tms, axis=0)

    def _refresh_attached_states(self):
        local_attached = torch.norm(self.gripper_pos.reshape(self.num_envs, 1, 3).repeat(1, len(self._attachable_points), 1) - self._attachable_points, p=2, dim=2)
        is_local_attached = local_attached < 0.1
        local_attached_prop = torch.rand_like(local_attached, device=self.device) * is_local_attached.float()
        local_attached_indices = torch.max(local_attached_prop, dim=1).indices
        local_attached_handle = self._attachable_handles[local_attached_indices]
        is_env_local_attached = is_local_attached.any(dim=1)
        
        self.is_attached = torch.where(self.is_attached == True, self.is_attached, is_env_local_attached)
        refresh_attached_body_index = (self.attached_body_handle == -1) * self.is_attached
        self.attached_body_handle = torch.where(refresh_attached_body_index, local_attached_handle, self.attached_body_handle)
        self.attached_info_indices = torch.where(refresh_attached_body_index, local_attached_indices, self.attached_info_indices)

        another_local_attached = torch.norm(self.gripper_another_pos.reshape(self.num_envs, 1, 3).repeat(1, len(self._attachable_points), 1) - self._attachable_points, p=2, dim=2)
        another_is_local_attached = another_local_attached < 0.1
        another_local_attached_prop = torch.rand_like(another_local_attached, device=self.device) * another_is_local_attached.float()
        another_local_attached_indices = torch.max(another_local_attached_prop, dim=1).indices
        another_local_attached_handle = self._attachable_handles[another_local_attached_indices]
        another_is_env_local_attached = another_is_local_attached.any(dim=1)
        
        self.another_is_attached = torch.where(self.another_is_attached == True, self.another_is_attached, another_is_env_local_attached)
        another_refresh_attached_body_index = (self.another_attached_body_handle == -1) * self.another_is_attached
        self.another_attached_body_handle = torch.where(another_refresh_attached_body_index, another_local_attached_handle, self.another_attached_body_handle)
        self.another_attached_info_indices = torch.where(another_refresh_attached_body_index, another_local_attached_indices, self.another_attached_info_indices)
    
    def get_states(self):
        panda_hand_body_handle = self.gym.find_actor_rigid_body_handle(self.envs[0], self.robots[0], 'robot0:sphere_link')
        another_panda_hand_body_handle = self.gym.find_actor_rigid_body_handle(self.envs[0], self.robots_another[0], 'robot1:sphere_link')
        rigid_bodies_states = self.rigid_body_states.clone().cpu().numpy()
        panda_hand_state = rigid_bodies_states[:, panda_hand_body_handle, :]
        another_panda_hand_state = rigid_bodies_states[:, another_panda_hand_body_handle, :]
        return {
            'panda_hand': panda_hand_state,
            'another_panda_hand': another_panda_hand_state,
            'rigid_bodies': rigid_bodies_states,
            'attached_body_handle': self.attached_body_handle.clone().cpu().numpy(),
            'attached_info_indices': self.attached_info_indices.clone().cpu().numpy(),
            'another_attached_body_handle': self.another_attached_body_handle.clone().cpu().numpy(),
            'another_attached_info_indices': self.another_attached_info_indices.clone().cpu().numpy(),
            'attach_info': self.objects_attach_info,
        }
    
    def step_plan(self, actions, actions1):
        attractor_pose = actions[0]
        gripper_effort = actions[1]
        another_attractor_pose = actions1[0]
        another_gripper_effort = actions1[1]
        # actions of pre_physics_step
        for i_env in range(self.num_envs):
            self.gym.set_attractor_target(self.envs[i_env], self.attractor_handles[i_env], attractor_pose)
            self.gym.set_attractor_target(self.envs[i_env], self.attractor_another_handles[i_env], another_attractor_pose)
        # action for gripper
        for i_env in range(self.num_envs):
            dof_effort = np.ones(self.num_robot_dofs, dtype=np.float32) * gripper_effort
            self.gym.apply_actor_dof_efforts(self.envs[i_env], self.robots[i_env], dof_effort)
            another_dof_effort = np.ones(self.num_robot_dofs, dtype=np.float32) * another_gripper_effort
            self.gym.apply_actor_dof_efforts(self.envs[i_env], self.robots_another[i_env], another_dof_effort)
        # debug viz
        if self.debug_viz:
            axes_geom = gymutil.AxesGeometry(0.3)
            self.gym.clear_lines(self.viewer)
            gymutil.draw_lines(axes_geom, self.gym, self.viewer, self.envs[0], attractor_pose)
            gymutil.draw_lines(axes_geom, self.gym, self.viewer, self.envs[0], another_attractor_pose)
        # step physics and render each frame
        # for i in range(self.control_freq_inv):
        for _ in range(2):
            self.render()
            self.gym.simulate(self.sim)
            self.gym.fetch_results(self.sim, True)
        # compute observations
        self.compute_observations()
        return self.extras

    def render_image(self, env_id=0):
        self.gym.render_all_camera_sensors(self.sim)
        self.gym.start_access_image_tensors(self.sim)
        camera_image = self.camera_images[env_id].clone()[..., :3]
        camera_image = camera_image.cpu().numpy()
        self.gym.end_access_image_tensors(self.sim)
        return camera_image
