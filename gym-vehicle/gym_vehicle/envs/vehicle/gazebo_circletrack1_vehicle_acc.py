import time
import sys
import rospy
import roslaunch
import numpy as np
import math

import gym
from gym.utils import seeding
from gym import utils, spaces
from gym_vehicle.envs import gazebo_env

from std_msgs.msg import Float64, Int32, String
from std_srvs.srv import Empty
from nav_msgs.msg import Path, Odometry
from geometry_msgs.msg import PoseStamped, Pose
from geometry_msgs.msg import TwistStamped, Twist
from geometry_msgs.msg import Quaternion
from sensor_msgs.msg import LaserScan

from gazebo_msgs.srv import SetModelState
from gazebo_msgs.msg import ModelState, ModelStates

DISPLAY_STATE = False

MAX_SPEED = 25.0  # m/sec; tune this
COLLISON_DIST = 5 # m
INIT_LANE_INDEX = 1
LAPS = 1

class GazeboCircletrack1VehicleAccEnv(gazebo_env.GazeboEnv):

    def __init__(self):
        # Launch the simulation with the given launchfile name
        gazebo_env.GazeboEnv.__init__(self, "GazeboCircletrack1VehicleAcc_v1.launch")

        self.base_path = None
        self.maps_s = None
        self.speeds = [0, 0]
        self.poses = [None, None]
        self.travel_dist = 0
        self.travel_time = 0
        self.time_stamp = None
        self.time_steps = 0
        self.prev_ego_pose = None
        self.prev_speed = 10
        self.change_lane_reward = 0
        self.cmp_dists = [80, 80]
        self.cmp_speeds = [0, 0]

        rospy.Subscriber('/base_path', Path, self.base_path_cb)

        rospy.Subscriber('/ego/twist', TwistStamped, self.ego_vel_cb)
        rospy.Subscriber('/mondeo/twist', TwistStamped, self.mondeo_vel_cb)

        rospy.Subscriber('/ego/current_pose', PoseStamped, self.ego_pose_cb)
        rospy.Subscriber('/mondeo/current_pose', PoseStamped, self.mondeo_pose_cb)

        self.cruise_speed_pub = rospy.Publisher('/ego/cruise_speed', Float64, queue_size=5)

        self.unpause = rospy.ServiceProxy('/gazebo/unpause_physics', Empty)
        self.pause = rospy.ServiceProxy('/gazebo/pause_physics', Empty)

        self._seed()

    def base_path_cb(self, msg):
        """ Store the given map """
        self.base_path = msg
        # frenet transform
        self.maps_s = []
        map_s = 0
        self.maps_s.append(map_s)
        map_x_prev = self.base_path.poses[0].pose.position.x
        map_y_prev = self.base_path.poses[0].pose.position.y
        for i in range(1, len(self.base_path.poses)):
            map_x = self.base_path.poses[i].pose.position.x
            map_y = self.base_path.poses[i].pose.position.y
            map_s += self.euclidean_distance(map_x, map_y, map_x_prev, map_y_prev)
            self.maps_s.append(map_s)
            map_x_prev = map_x
            map_y_prev = map_y

    def ego_vel_cb(self, data):
        self.speeds[0] = data.twist.linear.x

    def mondeo_vel_cb(self, data):
        self.speeds[1] = data.twist.linear.x

    def ego_pose_cb(self, data):
        self.poses[0] = data

    def mondeo_pose_cb(self, data):
        self.poses[1] = data

    def min_dang(self, dang):
        while dang > math.pi:
            dang -= 2.0 * math.pi
        while dang < -math.pi:
            dang += 2.0 * math.pi
        return dang

    # saturation function, values 0 to 1
    def speed_saturate(self, x):
        if x > MAX_SPEED:
            return MAX_SPEED
        if x < 0:
            return 0
        return x

    def quat2phi(self, quat):
        quat_z = quat.z
        quat_w = quat.w
        phi = 2.0 * math.atan2(quat_z, quat_w)
        return phi

    def phi2quat(self, phi):
        quaternion = Quaternion()
        quaternion.x = 0.0
        quaternion.y = 0.0
        quaternion.z = math.sin(phi / 2.0)
        quaternion.w = math.cos(phi / 2.0)
        return quaternion

    def track_distance(self, path_poses, p1, p2):
        dist = 0
        dl = lambda a, b: math.sqrt((a.x-b.x)**2 + (a.y-b.y)**2 + (a.z-b.z)**2)
        if p2 < p1:
            p2 += len(path_poses)
        for i in range(p1, p2+1):
            dist += dl(path_poses[p1%len(path_poses)].pose.position, path_poses[i%len(path_poses)].pose.position)
            p1 = i
        return dist

    def euclidean_distance(self, x1, y1, x2, y2):
        return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)

    def closest_waypoint_index(self, x, y, poses):
        closest_len = 100000  # large number
        closest_waypoint = 0

        for i in range(len(poses)):
            map_x = poses[i].pose.position.x
            map_y = poses[i].pose.position.y
            dist = self.euclidean_distance(x, y, map_x, map_y)
            if dist < closest_len:
                closest_len = dist
                closest_waypoint = i

        return closest_waypoint

    def next_waypoint(self, x, y, psi, poses):
        closest_waypoint = self.closest_waypoint_index(x, y, poses)

        map_x = poses[closest_waypoint].pose.position.x
        map_y = poses[closest_waypoint].pose.position.y

        heading = math.atan2((map_y-y), (map_x-x))

        angle = abs(psi-heading)
        angle = min(2*math.pi - angle, angle);

        if angle > math.pi/4:
          closest_waypoint += 1
          if closest_waypoint == len(poses):
            closest_waypoint = 0

        return closest_waypoint

    # Transform from Cartesian x,y coordinates to Frenet s,d coordinates
    # vector<double> getFrenet(double x, double y, double theta, const vector<double> &maps_x, const vector<double> &maps_y)
    def get_frenet(self, x, y, psi, poses):
        next_wp = self.next_waypoint(x, y, psi, poses)

        prev_wp = next_wp-1
        if next_wp == 0:
            prev_wp  = len(poses)-1

        n_x = poses[next_wp].pose.position.x - poses[prev_wp].pose.position.x
        n_y = poses[next_wp].pose.position.y - poses[prev_wp].pose.position.y
        x_x = x - poses[prev_wp].pose.position.x
        x_y = y - poses[prev_wp].pose.position.y

        # find the projection of x onto n
        proj_norm = (x_x*n_x+x_y*n_y)/(n_x*n_x+n_y*n_y)
        proj_x = proj_norm*n_x
        proj_y = proj_norm*n_y

        frenet_d = self.euclidean_distance(x_x, x_y, proj_x, proj_y)

        # see if d value is positive or negative by comparing it to a center point

        center_x = 0 - poses[prev_wp].pose.position.x
        center_y = 36.6 - poses[prev_wp].pose.position.y
        center_to_pos = self.euclidean_distance(center_x, center_y, x_x, x_y)
        center_to_ref = self.euclidean_distance(center_x, center_y, proj_x, proj_y)

        if center_to_pos <= center_to_ref:
            frenet_d *= -1

        # calculate s value
        frenet_s = 0
        for i in range(prev_wp):
            frenet_s += self.euclidean_distance(poses[i].pose.position.x, poses[i].pose.position.y, poses[i+1].pose.position.x, poses[i+1].pose.position.y)

        frenet_s += self.euclidean_distance(0, 0, proj_x, proj_y)

        return frenet_s, frenet_d

    # Transform from Frenet s,d coordinates to Cartesian x,y
    def get_xy(self, s, d, maps_s, poses):
        prev_wp = -1

        while prev_wp < len(maps_s)-1 and s > maps_s[prev_wp+1]:
            prev_wp += 1

        wp2 = (prev_wp+1) % len(maps_s)

        heading = math.atan2((poses[wp2].pose.position.y-poses[prev_wp].pose.position.y), (poses[wp2].pose.position.x-poses[prev_wp].pose.position.x))
        # the x,y,s along the segment
        seg_s = s-maps_s[prev_wp]

        seg_x = poses[prev_wp].pose.position.x + seg_s*math.cos(heading)
        seg_y = poses[prev_wp].pose.position.y + seg_s*math.sin(heading)

        perp_heading = heading-math.pi/2

        x = seg_x + d*math.cos(perp_heading)
        y = seg_y + d*math.sin(perp_heading)

        return x, y

    def d_to_ilane(self, d):
        if d >= 0 and d < 4:
            ilane = 0
        elif d >= 4 and d < 8:
            ilane = 1
        elif d >=8 and d < 12:
            ilane = 2
        else:
            # print("error lane index: d = %f" % d)
            ilane = None
        return ilane

    def compute_delta_s(self, pose1, pose2):
        p1 = self.closest_waypoint_index(pose1.pose.position.x, pose1.pose.position.y, self.base_path.poses)
        p2 = self.closest_waypoint_index(pose2.pose.position.x, pose2.pose.position.y, self.base_path.poses)
        dist1 = self.track_distance(self.base_path.poses, p1, p2)
        dist2 = self.track_distance(self.base_path.poses, p2, p1)
        if dist1 < dist2:
            return dist1
        else:
            return -dist2

    def check_relative_postion(self, delta_s, d1, d2):
        ilane1 = self.d_to_ilane(d1)
        ilane2 = self.d_to_ilane(d2)

        if ilane1 is None or ilane2 is None:
            return -1

        if ilane1 == -1 or ilane2 == -1:
            return None

        if ilane2 == ilane1:
            if delta_s > 0:
                return 1
            else:
                return 0
        else:
            return -1

    def construct_state(self):
        self.cmp_dists = [80, 80]
        self.cmp_speeds = [0, 0]

        state = [1.0, 1.0, 0]

        if self.base_path == None:
            return  state, False

        for i in range(2):
            if self.poses[i] == None:
                return state, False

        ss = []
        dd = []
        for i in range(2):
            x = self.poses[i].pose.position.x
            y = self.poses[i].pose.position.y
            psi = self.quat2phi(self.poses[i].pose.orientation)
            s, d = self.get_frenet(x, y, psi, self.base_path.poses)
            ss.append(s)
            dd.append(d)

        ego_s = ss[0]
        ego_d = dd[0]

        for i in range(1,2):
            s = ss[i]
            d = dd[i]
            delta_s = self.compute_delta_s(self.poses[0], self.poses[i])
            rp = self.check_relative_postion(delta_s, ego_d, d)
            if rp == -1:
                continue
            if abs(delta_s) < abs(self.cmp_dists[rp]):
                self.cmp_dists[rp] = abs(delta_s)
                self.cmp_speeds[rp] = self.speeds[i] - self.speeds[0]

        # # a car is coming backward
        # if abs(self.cmp_dists[0]) < 40:
        #     # ************************************************
        #     # Pause simulation to make observation
        #     # ************************************************
        #     rospy.wait_for_service('/gazebo/pause_physics')
        #     try:
        #         self.pause()
        #     except rospy.ServiceException as e:
        #         print("/gazebo/pause_physics service call failed")

        #     # ************************************************
        #     # define initial pose for later use
        #     # ************************************************
        #     mondeo_s = ego_s + 40
        #     mondeo_d = ego_d
        #     new_x, new_y = self.get_xy(mondeo_s, mondeo_d, self.maps_s, self.base_path.poses)
        #     index = self.closest_waypoint_index(new_x, new_y, self.base_path.poses)  
        #     next_x = self.base_path.poses[(index+1)/len(self.base_path.poses)].pose.position.x
        #     next_y = self.base_path.poses[(index+1)/len(self.base_path.poses)].pose.position.y

        #     new_pose = Pose()
        #     new_pose.position.x = new_x
        #     new_pose.position.y = new_y
        #     new_pose.position.z = 0.0
        #     new_pose.orientation = self.phi2quat(math.atan2(next_y-new_y, next_x-new_x) - math.pi/2)

            # # ************************************************
            # # set initial model state
            # # ************************************************
            # model_state_x = ModelState()
            # model_state_x.model_name = "mondeo"
            # model_state_x.pose = new_pose
            # rospy.wait_for_service('/gazebo/set_model_state')
            # try:
            #     set_model_state = rospy.ServiceProxy('/gazebo/set_model_state', SetModelState)
            #     ret = set_model_state(model_state_x)
            #     # print(ret.status_message)
            # except rospy.ServiceException as e:
            #     print("Service \'set_model_state\' call failed: %s" % e)

            # # ************************************************
            # # Unpause simulation to make observation
            # # ************************************************
            # rospy.wait_for_service('/gazebo/unpause_physics')
            # try:
            #     self.unpause()
            # except rospy.ServiceException as e:
            #     print("/gazebo/unpause_physics service call failed")

        done = False

        if DISPLAY_STATE:
            print("\n")
            print("|-- Current State --|")
            print("| Compared Dist:                                |")
            print("| %f \t|" % (self.cmp_dists[1]))
            print("|----- Ego -----|")
            print("| %f \t|" % (self.cmp_dists[0]))
            print("| Compared Speed:                               |")
            print("| %f \t|" % (self.cmp_speeds[1]))
            print("| %f \t|" % (self.speeds[0]))
            print("| %f \t|" % (self.cmp_speeds[0]))
            print("|-------------------|")
            print("| Current Lane: %d \t|" % (self.d_to_ilane(ego_d)))
            print("| Travel Distance: %f \t|" % (self.travel_dist))
            print("| Travel Time: %f \t|" % (self.travel_time))
            print("| Current Speed: %f \t|" % (self.speeds[0]))
            print("| Average Speed: %f \t|" % (0 if self.travel_time == 0 else self.travel_dist/self.travel_time))
            print("|-------------------|")

        if abs(self.cmp_dists[1]) < 10:
            print("Collision detected!")
            done = True  

        for i in range(2):
            if self.poses[i].pose.position.z > 0.5:
                print("Car %d is turned over!" % (i))
                done = True

        if ego_d < 4 or ego_d >= 8:
            print("Off lane!")
            done = True

        state = [self.speeds[0]/MAX_SPEED, self.cmp_dists[1]/80, self.cmp_speeds[1]/MAX_SPEED]

        return state, done

    def action_names(self, action):
        action_move_forward = ["Accelerate +2.0 m/s",
                               "Accelerate +1.0 m/s",
                               "Keep current speed",
                               "Decelerate -1.0 m/s",
                               "Decelerate -2.0 m/s"]
        return action_move_forward[action]

    def _seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def _step(self, action):

        state, done = self.construct_state()

        # 9 actions
        speed_cmd = self.speeds[0]
        # speed_cmd = self.prev_speed
        add_on = [+2.0, +1.0, 0, -1.0, -2.0]

        # print("cmd_speed = %f" % cmd_speed)
        speed_cmd = self.speed_saturate(speed_cmd + add_on[action])
        self.cruise_speed_pub.publish(speed_cmd)

        # self.prev_speed = speed_cmd

        if abs(self.cmp_dists[0]) < 10:
            print("Too slow!")
            return np.asarray(state), 0, True, {}

        # 9 actions
        reward = 0
        if done:
            reward += -100
            self.travel_dist = 0
            self.travel_time = 0
            self.prev_ego_pose = None
            self.time_steps = 0
            return np.asarray(state), reward, done, {}

        if self.prev_ego_pose == None:
            acc_dist = 0
        else:
            x0, y0 = self.prev_ego_pose.pose.position.x, self.prev_ego_pose.pose.position.y
            x1, y1 = self.poses[0].pose.position.x, self.poses[0].pose.position.y
            acc_dist = self.euclidean_distance(x0, y0, x1, y1)
        self.travel_dist += acc_dist
        self.prev_ego_pose = self.poses[0]

        if self.time_stamp is None:
            self.travel_time = 0
        else:
            self.travel_time += rospy.get_time() - self.time_stamp
        self.time_stamp = rospy.get_time()

        # speed
        # reward += 1.0 * self.speeds[0]

        # by acc_dist
        reward += 1.0 * acc_dist

        reward += -2.0 * abs(action - len(add_on)/2)

        # reward += 2 * self.speeds[0]/MAX_SPEED

        # self.time_steps += 1

        # if state[0] <= 10:
        #     reward += -100 / state[0]

        # delta_s = self.compute_delta_s(self.poses[0], self.poses[1])
        # if delta_s > 60:
        #     done = True

        # if DISPLAY_STATE:
        #     print("| Action: %s\t|" % self.action_names(action))
        #     print("| Reward: %f \t\t|" % reward)
        #     print("|-------------------|")

        # if self.travel_time >= 40.0 * LAPS:
        #     print("Time Out!")
        #     done = True
        #     self.travel_dist = 0
        #     self.travel_time = 0

        if self.time_steps > 500:
            print("Safely done! :D")
            # reward += 10000
            done = True
            self.travel_dist = 0
            self.travel_time = 0
            self.prev_ego_pose = None
            self.time_steps = 0

        return np.asarray(state), reward, done, {}

    def _reset(self):
        '''
        Resets the state of the environment and returns an initial observation.
        '''

        # ************************************************
        # Pause simulation to make observation
        # ************************************************
        rospy.wait_for_service('/gazebo/pause_physics')
        try:
            self.pause()
        except rospy.ServiceException as e:
            print("/gazebo/pause_physics service call failed")

        # ************************************************
        # define initial pose for later use
        # ************************************************
        init_pose0 = Pose()
        init_pose0.position.x = 42.6
        init_pose0.position.y = 36.6
        init_pose0.position.z = 0.0
        quat0 = self.phi2quat(1.57)
        init_pose0.orientation = quat0

        init_pose1 = Pose()
        init_pose1.position.x = 0.0
        init_pose1.position.y = 79.2
        init_pose1.position.z = 0.0
        quat1 = self.phi2quat(3.14)
        init_pose1.orientation = quat1

        init_twist0 = Twist()
        init_twist0.linear.x = 10.0

        init_twist1 = Twist()
        init_twist1.linear.x = 10.0

        # ************************************************
        # set initial model state
        # ************************************************
        model_state0 = ModelState()
        model_state0.model_name = "ego"
        model_state0.pose = init_pose0
        # model_state0.twist = init_twist0
        rospy.wait_for_service('/gazebo/set_model_state')
        try:
            set_model_state = rospy.ServiceProxy('/gazebo/set_model_state', SetModelState)
            ret = set_model_state(model_state0)
            # print(ret.status_message)
        except rospy.ServiceException as e:
            print("Service \'set_model_state\' call failed: %s" % e)

        model_state1 = ModelState()
        model_state1.model_name = "mondeo"
        model_state1.pose = init_pose1
        # model_state1.twist = init_twist1
        rospy.wait_for_service('/gazebo/set_model_state')
        try:
            set_model_state = rospy.ServiceProxy('/gazebo/set_model_state', SetModelState)
            ret = set_model_state(model_state1)
            # print(ret.status_message)
        except rospy.ServiceException as e:
            print("Service \'set_model_state\' call failed: %s" % e)

        # ************************************************
        # Unpause simulation to make observation
        # ************************************************
        rospy.wait_for_service('/gazebo/unpause_physics')
        try:
            self.unpause()
        except rospy.ServiceException as e:
            print("/gazebo/unpause_physics service call failed")

        # ************************************************
        # construct state
        # ************************************************
        # state, done = self.construct_state()
        state = [30.0, 10.0, 0]

        return np.asarray(state)
