import math
import os.path
import timeit
import numpy as np
from typing import cast, Optional
from collections import deque

# Import cpp classes
from hybridastar_planning_lib import AStar, HybridAStar, CollisionChecker, Cartographing, Smoother, UtilCpp

# Import other python modules
from . import util
from .logger_setup import Logger
from .data_structures import PatchInfo, PathHistory, VehConfig, WayType
from .data_structures import Path, NodeHybrid, NodeDisc, Vehicle, PoseDouble, PointDouble, PointInt, Minipatch
from .state_machine import *


class PathPlanning:
    def __init__(self, veh_config: VehConfig, lib_share_dir: str):

        self.minipatches = None
        config = util.load_lib_config(lib_share_dir)
        self.path2data = lib_share_dir + "/data"

        self.veh_config: VehConfig = veh_config
        self.logger: Logger = Logger(self)

        self.is_sim: bool = False

        # Gm params
        self.GM_DIM: int = config["GM_DIM"]  # only for sim, is used to specify the distance to the border of a patch
        self.GM_RES: float = config["GM_RES"]
        self.GM_DIST: float = self.GM_DIM * self.GM_RES
        self.PADDING_DIST: int = config["PADDING_DIST"]
        self.MAX_PATCH_INS_DIST: int = config["MAX_PATCH_INS_DIST"]

        # General planning params
        self.MIN_COLL_DIST: int = config["MIN_COLL_DIST"]  # [m] distance of collision under which replanning is forced
        # distance in astar grid under which cyclic replanning is prevented
        self.ENV_UPDATE_T: float = config["ENV_UPDATE_T"]
        self.GOAL_DIST: float = config["GOAL_DIST"]
        self.GOAL_ANGLE: float = config["GOAL_ANGLE"]
        self.MIN_REM_EL: int = config["MIN_REM_EL"]

        # Path planner params
        self.INTERP_RES: float = config["INTERP_RES"]  # [m]
        self.PLANNER_RES: float = config["PLANNER_RES"]

        # waypoint constants
        # [m] distance in idx under which the final goal is the actual goal
        self.MAX_DIST4WAYPOINTS: int = config["MAX_DIST4WAYPOINTS"]
        # [m] idx at which to draw waypoint from
        self.WAYPOINT_DIST: int = config["WAYPOINT_DIST"]
        self.WAYPOINT_TYPE: WayType = config["WAYPOINT_TYPE"]
        self.WAYPOINT_DIST_F: int = config["WAYPOINT_DIST_F"]
        self.MAX_DIST4REPLAN: float = config["MAX_DIST4REPLAN"]
        self.DIV_DISTANCE: float = config["DIV_DISTANCE"]
        self.KEEP_PATH_RATIO: float = config["KEEP_PATH_RATIO"]

        if self.MAX_DIST4WAYPOINTS < self.WAYPOINT_DIST:
            self.logger.log_error("Invalid parameters!")
            self.logger.log_error("The maximum waypoint distance must be higher than the sampling waypoint dist!")
            raise Exception("")

        # Planner variables
        self.new_goal: bool = False
        self.to_final_pose: bool = False
        self.max_patch_dist = config["MAX_DIST2PATCH"]  # can be changed
        self.t_idx: int = 0
        self.sim_idx: int = 0
        self.ego_v: float = 0
        self.ego_idx: int = 0
        self.ego_utm: Optional[PoseDouble] = None
        self.ego_utm_patch: Optional[PoseDouble] = None
        # self.goal_just_reached: bool = False
        self.reset_patch: bool = False
        self.divergent_dist: float = np.inf
        self.distance_since_last_replanning: float = 0
        self.distance2goal: float = np.inf
        self.dist2_end_of_path: float = np.inf
        self.coll_idx: int = -1
        self.path_id: int = 0
        self.shift_idx: Optional[int] = None
        self.last_env_calculation: float = 0
        self.time_now: float = 0
        self.time_goal_reached: float = 0

        self.lane_filename = lib_share_dir + "/data/" + "lane_graph.npy"

        # Goal objects
        self.goal_utm_patch: Optional[PoseDouble] = None
        self.rec_goal_utm_patch: Optional[PoseDouble] = None
        self.rec_goal_collides: bool = False
        self.rec_goal_utm: Optional[PoseDouble] = None
        self.goal_utm: Optional[PoseDouble] = None
        self.test_goal_pose: Optional[PoseDouble] = None

        # Path Planner objects
        self.astar_path: Optional[list] = []
        self.path: Optional[Path] = None
        self.path_utm: Optional[Path] = None
        self.proj_ego: Optional[PoseDouble] = None
        self.ego_node: Optional[NodeHybrid] = None
        self.plan_start_node: Optional[NodeHybrid] = None
        self.current_goal_node: Optional[NodeHybrid] = None
        self.global_goal_node: Optional[NodeHybrid] = None
        self.coll_point: Optional[PointDouble] = None
        self.overall_state = OverallState()

        # Environment variables
        self.patch_info: Optional[PatchInfo] = None

        # Configure vehicle
        self.reinit_vehicle(has_capsule=self.veh_config.has_capsule)

        # The patch_info will be overwritten later on create patch
        patch_dim: int = 1000
        patch_utm_origin: PointDouble = PointDouble(0, 0)
        HybridAStar.initialize(patch_dim, patch_utm_origin, lib_share_dir)

        # Visualization variables
        self.history_len: int = 1000
        self.hastar_cycle_times = deque(maxlen=self.history_len)
        self.planning_cycle_times = deque(maxlen=self.history_len)
        self.driven_path: PathHistory = PathHistory(self.history_len)

    def set_sim(self, is_sim: bool) -> None:
        self.is_sim = is_sim
        HybridAStar.setSim(self.is_sim)

    def reinit_vehicle(self, has_capsule: bool = False) -> None:

        # Change disk configurations and distance to back depending on container state
        current_lb = self.veh_config.lb if not has_capsule else self.veh_config.lb_extra
        Vehicle.initialize(self.veh_config.max_steer, self.veh_config.wb, self.veh_config.lf,
                           current_lb, self.veh_config.width, self.veh_config.is_ushift)

    def check_utm_pose(self, pose: PoseDouble) -> bool:
        if self.patch_info is None:
            return False

        pose_patch = UtilCpp.utm2patch_utm(pose)
        return CollisionChecker.checkPoseVariant(pose_patch)

    def check_start_end_pos(self) -> tuple[bool, bool, bool]:
        """
        Check if start and/or end position collides
        :return:
        """
        ego_save: bool = True
        rec_goal_save: bool = True
        goal_save: bool = True

        if self.ego_utm_patch:
            if not CollisionChecker.checkPose(self.ego_utm_patch):
                self.logger.log_error("Ego position is not collision free")
                ego_save = False

        if self.rec_goal_utm_patch:
            self.rec_goal_collides = False
            if not CollisionChecker.checkPose(self.rec_goal_utm_patch):
                self.logger.log_warning("Received goal is not collision free")
                rec_goal_save = False
                self.rec_goal_collides = True

        if self.goal_utm_patch:
            if not CollisionChecker.checkPose(self.goal_utm_patch):
                self.logger.log_warning("Goal is not collision free")
                goal_save = False

        return ego_save, rec_goal_save, goal_save

    def get_waypoint_from_path(self) -> NodeHybrid:
        """
        Extract a waypoint from the discrete astar path which will be a node that the path planning algorithm will
        approximately go to
        """

        # Get index of Waypoint
        idx_way: int = int(round(self.WAYPOINT_DIST / self.PLANNER_RES))

        # Waypoint must obviously be within astar path
        # waypoint is at least the waypoint distance away but at maximum at end of path
        path_length: int = len(self.astar_path[0])
        idx_way = min(path_length - 1, idx_way)

        # Extract point
        x_idx = self.astar_path[0][idx_way]
        y_idx = self.astar_path[1][idx_way]

        # Calculate yaw
        x_idx_m = self.astar_path[0][idx_way - 1]
        y_idx_m = self.astar_path[1][idx_way - 1]
        yaw = np.arctan2(y_idx - y_idx_m, x_idx - x_idx_m)

        # Create node
        waypoint: PoseDouble = PoseDouble(x_idx * self.PLANNER_RES, y_idx * self.PLANNER_RES, yaw)
        return HybridAStar.createNode(waypoint, 0)

    def is_goal_reached(self):
        ego_point = [self.ego_utm_patch.x, self.ego_utm_patch.y]
        goal_point = [self.goal_utm_patch.x, self.goal_utm_patch.y]
        nb_path_el_ahead: int = len(self.path.x_list[self.ego_idx:])
        if np.allclose(ego_point, goal_point, atol=self.GOAL_DIST) and \
                util.angles_equal_0_2pi(self.goal_utm_patch.yaw, self.ego_utm_patch.yaw, tol=self.GOAL_ANGLE) and \
                nb_path_el_ahead <= self.MIN_REM_EL:
            self.reset_data()
            self.logger.log_success("Goal reached!")
            self.time_goal_reached = self.time_now
            return True
        return False

    def was_goal_just_reached(self) -> bool:
        if (self.time_now - self.time_goal_reached) < 1:
            return True
        else:
            return False

    def check_goal_or_end_of_path(self) -> None:
        """
        :param ego_utm_patch:
        :param goal_utm_patch:
        :return:
        """

        if self.goal_utm_patch is None:
            return

        # Check if goal was reached
        self.is_goal_reached()

    def get_distance_along_disc_path(self) -> float:
        """
        calculates the distance over the current path
        """
        return UtilCpp.getPathLength(self.astar_path[0], self.astar_path[1]) * self.PLANNER_RES

    def distance_too_high(self) -> bool:
        """
        Checks if distance to previously planned path is too high
        """
        # Calculate distance to path, if it's too high, do replanning
        if self.path is not None:
            self.proj_ego, idx, min_dist = HybridAStar.projEgoOnPath(self.ego_utm_patch, self.path, self.ego_idx)
            if min_dist > self.MAX_DIST4REPLAN:
                # self.logger.log_error("Distance to path:", min_dist)
                self.path = None
                self.path_utm = None
                return True
        return False

    def check_replanning(self) -> bool:
        """
        Evaluates if replanning should be triggered
        :return:
        """

        replan: bool = False

        if self.goal_utm_patch is None:
            return False

        if self.overall_state.ego_s == EgoOnPathState.GOAL:
            return False

        if self.check4new_waypoint():
            self.logger.log_info("New waypoint or final goal -> Replan")
            replan = True

        if self.new_goal:
            self.logger.log_info("New goal received -> Replan")
            self.new_goal = False
            replan = True

        if self.evaluate_collisions():
            self.logger.log_info("Close collision -> Replan")
            replan = True

        if self.divergent_dist != np.inf:
            self.logger.log_info("Diverged -> Replan")
            replan = True

        if self.distance_too_high():
            self.logger.log_info("Distance to path too far!-> Replan")
            replan = True

        return replan

    def evaluate_collisions(self) -> bool:
        """
        Set planning state depending on the distance of the collision
        Args:

        Returns:

        """
        if self.path is not None:
            if self.overall_state.path_s == PathState.COLLIDES:
                # Calculate distance to collision
                path2collision_x = self.path.x_list[self.ego_idx:self.ego_idx + self.coll_idx]
                path2collision_y = self.path.y_list[self.ego_idx:self.ego_idx + self.coll_idx]
                distance2collision = UtilCpp.getPathLength(path2collision_x, path2collision_y)

                if distance2collision < self.MIN_COLL_DIST:
                    self.path = None
                    return True
        return False

    def check4collisions(self) -> None:
        """
        Check if path from previous timestep is collision free, and set planning state

        """
        if self.path is not None:
            # Check collisions in current local map
            self.coll_idx = CollisionChecker.getPathCollisionIndex(self.path.x_list[self.ego_idx:],
                                                                   self.path.y_list[self.ego_idx:],
                                                                   self.path.yaw_list[self.ego_idx:])

            self.coll_point = None
            if self.coll_idx != -1:
                self.coll_point = PointDouble(self.path.x_list[self.ego_idx + self.coll_idx],
                                              self.path.y_list[self.ego_idx + self.coll_idx])

            self.overall_state.path_s = PathState.SAFE if self.coll_idx == -1 else PathState.COLLIDES

    def create_patch(self, goal_utm: PoseDouble) -> None:
        """
        Creates a patch_info around the start and goal to plan on
        Args:
            goal_utm:

        Returns:

        """

        # Points of start and goal that span patch_info
        min_x = min(self.ego_utm.x, goal_utm.x)
        min_y = min(self.ego_utm.y, goal_utm.y)
        max_x = max(self.ego_utm.x, goal_utm.x)
        max_y = max(self.ego_utm.y, goal_utm.y)

        # lower left and upper right corners of patch_info which is padded around start and goal
        lower_left = PointDouble(min_x, min_y)
        upper_right = PointDouble(max_x, max_y)
        distances = (abs(lower_left.x - upper_right.x), abs(lower_left.y - upper_right.y))
        max_dist = np.max(distances)
        patch_dim_utm = max_dist + 2 * self.PADDING_DIST
        patch_dim_gm = UtilCpp.utm2grid_round(patch_dim_utm)
        middle_point = PointDouble(distances[0] / 2 + lower_left.x, distances[1] / 2 + lower_left.y)
        # lower left and upper right corner
        origin_utm: PointDouble = middle_point - patch_dim_utm / 2

        # Triggers reinitiation of all data structures in HybridAStar, AStar and CollisionChecker
        HybridAStar.reinit(origin_utm, patch_dim_gm)

        # if cartographing is necessary = in simulation
        if self.is_sim and self.patch_info is not None:
            Cartographing.loadPrevPatch(self.patch_info.origin_utm, origin_utm)

        # New clean patch_info of zeros for patch_info
        self.patch_info = PatchInfo(origin_utm, patch_dim_gm, patch_dim_utm)

        # Update coordinates of ego and goal on patch_info
        self.set_coords_on_patch()

        self.load_graph()

        self.path_id = self.t_idx

        self.insert_minipatches(only_nearest=False, only_new=False)

    def combine_paths(self, new_path: Path) -> None:
        """
        Add the new path on top of the previous path
        :param new_path:
        :return:
        """
        # Concatenate paths from ego to shift with new one
        start_idx: int = self.ego_idx
        end_idx: int = self.ego_idx + self.shift_idx

        new_path.x_list = self.path.x_list[start_idx:end_idx] + new_path.x_list
        new_path.y_list = self.path.y_list[start_idx:end_idx] + new_path.y_list
        new_path.yaw_list = self.path.yaw_list[start_idx:end_idx] + new_path.yaw_list
        new_path.direction_list = self.path.direction_list[start_idx:end_idx] + new_path.direction_list
        # new_path.cost_list = self.path.cost_list[start_idx:end_idx] + new_path.cost_list
        new_path.types = self.path.types[start_idx:end_idx] + new_path.types
        ratio_prev_path: float = (end_idx - start_idx) / len(self.path.x_list)
        new_path.cost = self.path.cost * ratio_prev_path + new_path.cost

    def get_shifted_planning_nodes(self) -> None:
        """
        Start node for planning is shifted from the ego position
        :return:
        """

        # If not path has been calculated, plan from ego
        if self.path is None:
            self.plan_start_node = self.ego_node
            return

        # 2) divergent dependant shift
        div_dist_m: float = np.inf
        if self.divergent_dist != np.inf:
            div_dist_m: float = self.divergent_dist * self.KEEP_PATH_RATIO

        # 3) shift depending on distance of collision
        # Start index of planning is at maximum halfway to collision
        coll_dist_m: float = np.inf
        if self.coll_idx != -1:
            coll_dist_m = self.coll_idx * self.INTERP_RES * self.KEEP_PATH_RATIO

        # 4) no divergence of ref path, start from end of path
        std_start: float = self.KEEP_PATH_RATIO * self.dist2_end_of_path

        # Resulting shift
        plan_shift_m: float = np.min([div_dist_m, coll_dist_m, std_start])
        self.shift_idx = int(plan_shift_m / self.INTERP_RES)

        # Start index cannot be higher than length
        nb_elements_in_front: int = len(self.path.x_list) - self.ego_idx
        self.shift_idx = min(self.shift_idx, nb_elements_in_front - 1)

        # No shift, planning node is ego node
        if self.shift_idx == 0:
            self.plan_start_node = self.ego_node
            return

        # Create new node for shift
        start_idx = self.ego_idx + self.shift_idx
        plan_start_utm: PoseDouble = PoseDouble(self.path.x_list[start_idx],
                                                self.path.y_list[start_idx],
                                                self.path.yaw_list[start_idx])

        yaws = self.path.yaw_list[start_idx:start_idx + 2]
        steer: float = 0.0
        if len(yaws) > 1:
            # Calculate curvature to get steering back from path
            curvature = util.get_curvatures(yaws, ds=self.INTERP_RES)
            steer = math.atan(curvature[0] * self.veh_config.wb)

        # Create node from point on path
        self.plan_start_node = HybridAStar.createNode(plan_start_utm, steer)

    def set_goal(self) -> None:
        """
        Sets the goal depending on the use of waypoints and the distance to the goal
        :return:
        """
        # No waypoints are used, final goal is the goal
        if self.WAYPOINT_TYPE == WayType.ASTAR_PATH and self.distance2goal > self.MAX_DIST4WAYPOINTS:
            self.current_goal_node = self.get_waypoint_from_path()
        else:
            self.current_goal_node = self.global_goal_node

    def analyse_vehicle_on_path(self, path: Path) -> float:
        """
        Finds the closest coordinate of the vehicle on the path and truncates the path behind the vehicle

        Returns:
            dist2end

        """
        if path is None:
            self.proj_ego = self.ego_utm_patch.copy()
            return 0.0

        if len(path.x_list) < 2:
            return 0.0

        self.proj_ego, ego_idx, min_dist = HybridAStar.projEgoOnPath(self.ego_utm_patch, path, self.ego_idx)

        # Previous ego index to current ego index = driven path
        driven_x = self.path.x_list[self.ego_idx:ego_idx + 1]
        driven_y = self.path.y_list[self.ego_idx:ego_idx + 1]
        driven_x, driven_y = UtilCpp.patch_utm2utm(driven_x, driven_y)

        driven_dist: float = UtilCpp.getPathLength(driven_x, driven_y)
        self.distance_since_last_replanning += driven_dist

        driven_x = driven_x[:-1]  # delete last, it is the first of the next
        driven_y = driven_y[:-1]  # delete last, it is the first of the next
        driven_yaw = self.path.yaw_list[self.ego_idx:ego_idx]
        driven_dir = self.path.direction_list[self.ego_idx:ego_idx]
        driven_has_capsule = [self.veh_config.has_capsule] * len(driven_dir)

        # save paths
        self.driven_path.set_max_len(self.history_len)
        self.driven_path.x_list.extend(driven_x)
        self.driven_path.y_list.extend(driven_y)
        self.driven_path.yaw_list.extend(driven_yaw)
        self.driven_path.direction_list.extend(driven_dir)
        self.driven_path.has_capsule.extend(driven_has_capsule)

        # Update ego index to new one
        self.ego_idx = ego_idx

        # Get distance to end of path
        x_in_front = self.path.x_list[self.ego_idx:]
        y_in_front = self.path.y_list[self.ego_idx:]

        dist2end: float = UtilCpp.getPathLength(x_in_front, y_in_front)

        return dist2end

    def is_out_of_patch(self, goal_pose: PoseDouble) -> bool:
        """
        Checks if a goal pose is outside of the current patch_info
        :param goal_pose:
        :return:
        """
        max_pos = max(goal_pose.x, goal_pose.y)
        min_pos = min(goal_pose.x, goal_pose.y)
        return max_pos > self.patch_info.dim_utm or min_pos < 0

    def analyse_new_goal(self) -> None:
        """

        Returns:

        """
        # The same goal was already received
        if self.goal_utm is not None:
            if self.rec_goal_utm.equal(self.goal_utm):
                return

        # Set the just received goal
        self.goal_utm = self.rec_goal_utm.copy()
        self.set_coords_on_patch()

        # Reset state
        self.overall_state = OverallState()
        # Plan to new goal
        self.new_goal = True
        self.path = None
        self.path_utm = None
        if self.WAYPOINT_TYPE == WayType.ASTAR_PATH:
            self.overall_state.goal_s = GoalState.APPROX_GOAL

        # Waypoint type is heur red one
        if self.WAYPOINT_TYPE == WayType.HEUR_RED:
            self.overall_state.goal_s = GoalState.EXACT_GOAL

    def is_near_border_of_patch(self, ego_utm_patch: PoseDouble) -> bool:
        """
        Detects if a position is near the border of the patch_info
        :param ego_utm_patch:
        :return:
        """
        max_pos = max(ego_utm_patch.x, ego_utm_patch.y)
        min_pos = min(ego_utm_patch.x, ego_utm_patch.y)

        is_near_border = max_pos > (self.patch_info.dim_utm - self.GM_DIST / 2) or min_pos < self.GM_DIST / 2

        return is_near_border

    def reset_data(self) -> None:
        """
        Removes the last goal and resets the state
        :return:
        """
        # reset goal data
        self.goal_utm = None
        self.rec_goal_utm = None
        self.goal_utm_patch = None
        self.rec_goal_utm_patch = None

        # reset paths
        self.path = None
        self.path_utm = None

        # Reset state
        self.overall_state = OverallState()
        self.overall_state.ego_s = EgoOnPathState.GOAL

        # reset patch around ego
        self.create_patch(self.ego_utm)

    def set_coords_on_patch(self) -> None:
        """
        Set Subtract the patch_info origin from the utm coords
        :return:
        """

        if self.patch_info is None:
            return

        # Shift utm coords by origin of patch_info
        self.ego_utm_patch = UtilCpp.utm2patch_utm(self.ego_utm)
        # Shift utm coords by origin of patch_info
        if self.rec_goal_utm is not None:
            self.rec_goal_utm_patch = UtilCpp.utm2patch_utm(self.rec_goal_utm)
        else:
            self.rec_goal_utm_patch = None

        if self.goal_utm is not None:
            self.goal_utm_patch = UtilCpp.utm2patch_utm(self.goal_utm)
        else:
            self.goal_utm_patch = None

    def process_meas_grid(self, local_map: Minipatch) -> None:
        """
        Parse meas grid from simulation
        """
        ego_gm_patch = UtilCpp.utm2grid_round(self.ego_utm_patch)
        x_lims, y_lims = util.get_current_map_lims(ego_gm_patch, local_map.width_)
        origin: PointInt = PointInt(x_lims[0], y_lims[0])

        Cartographing.cartograph(local_map.patch_, origin, local_map.width_)
        Cartographing.passLocalMap(origin, local_map.width_)

    def insert_minipatches(self, only_nearest: bool = False, only_new: bool = True) -> None:
        """
        Parse minipatches from gridfusion module
        """
        if isinstance(self.minipatches, dict):
            # TODO (Schumann) do this directly as a driver function in the node itself, not here
            CollisionChecker.insertMinipatches(self.minipatches, self.ego_utm.getPoint(), only_nearest, only_new)

        else:
            # Do cartographing with meas grid from sim
            # In simulation, the minipatches are one single grid, therefore it must be processed differently
            minipatch: Minipatch = cast(Minipatch, self.minipatches)

            self.process_meas_grid(minipatch)

        CollisionChecker.processSafetyPatch()

    def set_divergent_point(self, astar_path: Optional[list]) -> None:
        """
        Find point where navigation paths diverge to replan approximately from this point in the next step
        """
        self.divergent_dist = np.inf

        if self.astar_path:

            # save shift to improve comparisom
            matching_dist_x: float = 0
            matching_dist_y: float = 0
            # find indices where coordinates are closest
            matching_idx: int = -1
            min_dist: float = np.inf
            nb_elements: int = min(len(astar_path[0]), len(self.astar_path[0]))
            for idx_prev in range(nb_elements):
                dist_x = astar_path[0][0] - self.astar_path[0][idx_prev]
                dist_y = astar_path[1][0] - self.astar_path[1][idx_prev]
                dist = math.sqrt(dist_x ** 2 + dist_y ** 2)
                if dist < min_dist:
                    min_dist = dist
                    matching_idx = idx_prev
                    matching_dist_x = dist_x
                    matching_dist_y = dist_y
                    if dist < 1:
                        break

            # Paths do not match, this means new path begins behind prev. one - don't use divergence as a criteria
            if matching_idx == -1:
                return

            # calculate distance between points
            nb_elements -= matching_idx
            for idx in range(nb_elements - 1):
                # shift back to origin of previous one
                dist_x = astar_path[0][idx] - matching_dist_x - self.astar_path[0][idx + matching_idx]
                dist_y = astar_path[1][idx] - matching_dist_y - self.astar_path[1][idx + matching_idx]
                dist = math.sqrt(dist_x ** 2 + dist_y ** 2)

                if dist > self.DIV_DISTANCE:
                    self.divergent_dist = UtilCpp.getPathLength(astar_path[0][:idx],
                                                                astar_path[1][:idx]) * self.PLANNER_RES

                    break

    def parse_goal_message(self, goal_message) -> None:
        """
        Set goal or remove it depending on the provided goal message
        """
        # Parse goal message
        self.rec_goal_utm = None
        if goal_message is not None:
            if goal_message.remove:
                self.logger.log_success("Removing goal")
                self.reset_data()
            elif goal_message.pose is not None:
                self.logger.log_success("Goal received")
                self.rec_goal_utm = goal_message.pose.copy()
            else:
                # do nothing
                pass

    def patch_handling(self) -> None:
        """
        Create patches depending on the goal and ego positions
        """

        if self.reset_patch:
            self.create_patch(self.ego_utm)
            self.reset_patch = False
            return

        # 0) Create the first patch_info
        if self.patch_info is None:
            if self.rec_goal_utm:
                # self.logger.log_info("First patch around ego and rec goal")
                patch_span_point_2 = self.rec_goal_utm
            else:
                # self.logger.log_info("First patch around ego and ego")
                patch_span_point_2 = self.ego_utm

            self.create_patch(patch_span_point_2)
            return

        # Create a new patch_info
        # 1) Create new patch_info if ego is near the border of the current patch_info
        if self.is_near_border_of_patch(self.ego_utm_patch):
            if self.goal_utm:
                # self.logger.log_info("Ego pos is near border of patch. Creating a new patch_info around ego and goal")
                patch_span_point_2 = self.goal_utm
            else:
                # self.logger.log_info("Ego pos is near border of patch. Creating a new patch_info around ego")
                patch_span_point_2 = self.ego_utm

            self.create_patch(patch_span_point_2)
            return

        # 2) Create a new patch_info if goal is near border of the patch_info
        if self.rec_goal_utm_patch and self.is_near_border_of_patch(self.rec_goal_utm_patch):
            # self.logger.log_info("Goal is near border of patch. Creating a new patch_info")
            patch_span_point_2 = self.rec_goal_utm
            self.create_patch(patch_span_point_2)
            return

        # 3) Create new patch_info if goal is outside of current patch_info
        if self.rec_goal_utm_patch and self.is_out_of_patch(self.rec_goal_utm_patch):
            # self.logger.log_info("Goal is out of the patch_info. Creating a new patch around ego and goal position")
            patch_span_point_2 = self.rec_goal_utm
            self.create_patch(patch_span_point_2)
            return

    def goal_handling(self) -> None:
        """
        Check if goals are collision free and processes the new one, or removes the previous one
        """
        _, rec_goal_save, goal_save = self.check_start_end_pos()

        # Create node from received goal and save it
        self.new_goal = False
        if self.rec_goal_utm_patch is not None:
            if rec_goal_save or self.find_secondary_goal(rec=True):
                # Handle goals, might create a new patch_info
                self.analyse_new_goal()
                return

        # If goal exists and is not safe try to find a nearby one, otherwise reset path and
        if self.goal_utm is not None and not goal_save:
            if not self.find_secondary_goal(rec=False):
                self.reset_data()

    def find_secondary_goal(self, rec: bool) -> bool:

        if rec:
            valid_goal_pose = HybridAStar.getValidClosePose(self.ego_utm_patch, self.rec_goal_utm_patch)
        else:
            valid_goal_pose = HybridAStar.getValidClosePose(self.ego_utm_patch, self.goal_utm_patch)

        if valid_goal_pose is not None:
            if rec:
                self.logger.log_warning("A goal near just received goal was found!")
                # Set goal and set utm one, too
                self.rec_goal_utm_patch = valid_goal_pose.copy()
                self.rec_goal_utm = UtilCpp.patch_utm2utm(self.rec_goal_utm_patch)
            else:
                self.logger.log_warning("A goal near the previously received goal was found!")
                self.goal_utm_patch = valid_goal_pose.copy()
                self.goal_utm = UtilCpp.patch_utm2utm(self.goal_utm_patch)
            return True
        return False

    def env_handling(self, recalc=False) -> bool:
        """
        Recalculate the planning env if necessary
        """

        if self.is_sim:
            recalculate_env: bool = self.t_idx % 10 == 0
        else:
            recalculate_env: bool = self.t_idx == 0 or (self.time_now - self.last_env_calculation) > self.ENV_UPDATE_T

        if recalculate_env or recalc or self.new_goal:
            self.last_env_calculation = self.time_now
            self.insert_minipatches(only_nearest=False)

            if self.global_goal_node is None:
                HybridAStar.recalculateEnv(self.ego_node, self.ego_node)
            else:
                HybridAStar.recalculateEnv(self.global_goal_node, self.ego_node)

            astar_path: Optional[list] = AStar.getAstarPath(self.ego_node.x_index, self.ego_node.y_index)

            if astar_path is not None:
                # Check were astar_paths diverged
                self.set_divergent_point(astar_path)
                # set astar path
                self.astar_path = astar_path

        self.distance2goal = self.get_distance_along_disc_path()

        return recalculate_env

    def check4new_waypoint(self) -> bool:
        """
        Checks if there is a new waypoint to plan to
        """

        # self.to_final_pose = False

        # No waypoints are used, final goal is the goal
        if self.WAYPOINT_TYPE == WayType.NONE:
            self.to_final_pose = True
            self.overall_state.goal_s = GoalState.EXACT_GOAL
            return False

        # Trigger replanning because state switched to planning to exact goal
        if self.WAYPOINT_TYPE == WayType.ASTAR_PATH:
            if self.distance2goal < self.MAX_DIST4WAYPOINTS and self.overall_state.goal_s == GoalState.APPROX_GOAL:
                self.overall_state.goal_s = GoalState.EXACT_GOAL
                return True

        if self.WAYPOINT_TYPE == WayType.HEUR_RED:
            if self.distance2goal < self.MAX_DIST4WAYPOINTS and not self.to_final_pose:
                self.overall_state.goal_s = GoalState.EXACT_GOAL
                self.to_final_pose = True
                return True

        # Valid for all navigation types
        if self.distance2goal > self.MAX_DIST4WAYPOINTS:
            self.to_final_pose = False

            # Trigger replanning because vehicle moved certain distance along navigation path
            dist_limit: float = min(float(self.WAYPOINT_DIST_F), self.dist2_end_of_path / 2)
            if self.distance_since_last_replanning > dist_limit:
                return True

        return False

    def load_graph(self) -> bool:
        if self.patch_info is not None:
            HybridAStar.resetLaneGraph()
            if os.path.isfile(self.lane_filename):
                data = np.load(self.lane_filename)
                x_list = data[0, :]
                y_list = data[1, :]

                for x, y in zip(x_list, y_list):
                    pos = PointDouble(x, y)
                    HybridAStar.lane_graph_.addPoint(pos)

                HybridAStar.updateLaneGraph(self.patch_info.origin_utm, self.patch_info.dim_utm)
            return True
        return False

    def do_planning(self, ego_utm: PoseDouble, ego_v: float, goal_message, toggle_container: bool, minipatches: dict,
                    time_seconds: float) -> tuple:
        """
        Free space planning algorithm

        Args:
            ego_utm: Current position from ego motion
            ego_v:
            goal_message: Current goal in utm coordinates
            toggle_container: Command to drop or take container on next goal
            minipatches: Local Grid Map
            time_seconds:

        Returns:

        """

        # Save inputs
        self.ego_v = ego_v
        self.ego_utm: PoseDouble = ego_utm.copy()
        self.time_now = time_seconds
        self.minipatches = minipatches

        Vehicle.setPose(self.ego_utm)

        # Timing
        hastar_cycle_time = 0
        t0_planning = timeit.default_timer()

        self.parse_goal_message(goal_message)

        # Ensure correct coordinates on patch_info
        self.set_coords_on_patch()

        ################################################################################################################
        # Patch creation
        ################################################################################################################
        self.patch_handling()

        ################################################################################################################
        # Grid Data insertion
        ################################################################################################################
        # insert meas grid from simulation or the nearest minipatches
        self.insert_minipatches(only_nearest=True)

        ################################################################################################################
        # Validation of received goal and previous goal
        ################################################################################################################
        self.goal_handling()

        ################################################################################################################
        # Project to path and get travelled distance
        ################################################################################################################
        self.dist2_end_of_path = self.analyse_vehicle_on_path(self.path)

        ################################################################################################################
        # HAstar preparation
        ################################################################################################################
        # Create Nodes
        self.ego_node = HybridAStar.createNode(self.ego_utm_patch, 0)
        if self.goal_utm_patch is not None:
            self.global_goal_node = HybridAStar.createNode(self.goal_utm_patch, 0)
        else:
            self.global_goal_node = HybridAStar.createNode(self.ego_utm_patch, 0)

        ################################################################################################################
        # Recalculate the planning environment from time to time
        ################################################################################################################
        env_just_calculated = self.env_handling()

        ################################################################################################################
        # Check if previously calculated path collides and set planning state
        ################################################################################################################
        self.check4collisions()

        self.path_id: int = 0
        ################################################################################################################
        # Planning
        ################################################################################################################
        if self.check_replanning():
            # Reset distance and time since last replanning
            self.distance_since_last_replanning = 0.0

            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            # Recalculate the planning map potential field heuristics and the global distance heuristic
            # due to changes in the environment
            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            if not env_just_calculated:
                self.env_handling(recalc=True)

            self.set_goal()

            # Get start node to plan on
            self.get_shifted_planning_nodes()

            # Enable analytic solution to reach exact position
            do_analytic = self.overall_state.goal_s == GoalState.EXACT_GOAL

            t0_hastar = timeit.default_timer()

            new_path: Optional[Path] = HybridAStar.hybridAStarPlanning(self.ego_node,
                                                                       self.plan_start_node,
                                                                       self.current_goal_node,
                                                                       self.to_final_pose,
                                                                       do_analytic)

            t1_hastar = timeit.default_timer()
            hastar_cycle_time = t1_hastar - t0_hastar

            # No path was found!
            if new_path is None and self.path is None:
                self.logger.log_error("No path was found!")
                return self.path, self.path_id

            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            # Combine with previous path
            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            if self.path is not None:
                self.combine_paths(new_path)

            # Reset replanning state
            self.overall_state.repl_s = PlanningState.CYCLIC
            self.ego_idx = 0
            # set new paths
            self.path = new_path
            # Cast path to utm frame
            self.path_utm = UtilCpp.patch_utm2utm(self.path)
            self.path_id = self.t_idx

        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Check if goal was reached or end of path and if there are multiple goals
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Get state of planner on current path
        self.check_goal_or_end_of_path()

        # Timing
        t1_planning = timeit.default_timer()
        self.planning_cycle_times.append(t1_planning - t0_planning)
        self.hastar_cycle_times.append(hastar_cycle_time)

        self.t_idx += 1

        return self.path_utm, self.path_id
