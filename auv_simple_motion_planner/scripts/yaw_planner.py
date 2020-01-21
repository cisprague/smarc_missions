#!/usr/bin/python

# Copyright 2018 Nils Bore, Sriharsha Bhat (nbore@kth.se, svbhat@kth.se)
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from __future__ import division, print_function

import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from nav_msgs.msg import Path
from geometry_msgs.msg import PoseStamped, PointStamped
from move_base_msgs.msg import MoveBaseFeedback, MoveBaseResult, MoveBaseAction
import actionlib
import rospy
import tf
from sam_msgs.msg import ThrusterRPMs, ThrusterAngles
from std_msgs.msg import Float64, Header, Bool
import math

class YawPlanner(object):

    # create messages that are used to publish feedback/result
    _feedback = MoveBaseFeedback()
    _result = MoveBaseResult()
    
    def execute_cb(self, goal):

        rospy.loginfo("Goal received")

        success = True
        self.nav_goal = goal.target_pose.pose
        
        goal_point = PointStamped()
        goal_point.header.frame_id = "/world_utm"
        goal_point.header.stamp = rospy.Time(0)
        goal_point.point.x = self.nav_goal.position.x
        goal_point.point.y = self.nav_goal.position.y
        goal_point.point.z = self.nav_goal.position.z
        try:
            goal_point_local = self.listener.transformPoint("/world_local", goal_point)
            self.nav_goal.position.x = goal_point_local.point.x
            self.nav_goal.position.y = goal_point_local.point.y
            self.nav_goal.position.z = goal_point_local.point.z
        except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
            print ("Not transforming point to world local")
            pass
        
        rospy.loginfo('Nav goal in local %s ' % self.nav_goal.position.x)
        
        r = rospy.Rate(11.) # 10hz
        counter = 0
        while not rospy.is_shutdown() and self.nav_goal is not None:

            self.yaw_pid_enable.publish(True)
            # Preempted
            if self._as.is_preempt_requested():
                rospy.loginfo('%s: Preempted' % self._action_name)
                self._as.set_preempted()
                success = False
                self.nav_goal = None

                # Stop thrusters
                rpm = ThrusterRPMs()
                rpm.thruster_1_rpm = 0.
                rpm.thruster_2_rpm = 0.
                self.rpm_pub.publish(rpm)
                self.yaw_pid_enable.publish(False)
                break

            # Publish feedback
            if counter % 10 == 0:
                try:
                    (trans, rot) = self.listener.lookupTransform("/world_local", "sam/base_link", rospy.Time(0))
                except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                    rospy.loginfo("Error with tf")
                    continue

                pose_fb = PoseStamped()
                pose_fb.header.frame_id = "/world_local"
                pose_fb.pose.position.x = trans[0]
                pose_fb.pose.position.y = trans[1]
                pose_fb.pose.position.z = trans[2]
                self._feedback.base_position = pose_fb
                self._feedback.base_position.header.stamp = rospy.get_rostime()
                self._as.publish_feedback(self._feedback)
                rospy.loginfo("Sending feedback")

                #Compute yaw setpoint.
                xdiff = self.nav_goal.position.x - pose_fb.pose.position.x
                ydiff = self.nav_goal.position.y - pose_fb.pose.position.y
                yaw_setpoint = math.atan2(ydiff,xdiff)
                
            self.yaw_pub.publish(yaw_setpoint)
            rospy.loginfo("Yaw setpoint: %f", yaw_setpoint)

            # Thruster forward
            rpm = ThrusterRPMs()
            rpm.thruster_1_rpm = 400.
            rpm.thruster_2_rpm = 400.
            self.rpm_pub.publish(rpm)
            rospy.loginfo("Thrusters forward")

            counter += 1
            r.sleep()
        
        # Stop thruster
        rpm = ThrusterRPMs()
        rpm.thruster_1_rpm = 0.0
        rpm.thruster_2_rpm = 0.0
        self.rpm_pub.publish(rpm)
        #Stop yaw controller
        self.yaw_pid_enable.publish(False)
        rospy.loginfo('%s: Succeeded' % self._action_name)
        self._as.set_succeeded(self._result)


    def timer_callback(self, event):
        if self.nav_goal is None:
            rospy.loginfo("Nav goal is None!")
            return
        
        try:
            (trans, rot) = self.listener.lookupTransform("/world", self.base_frame, rospy.Time(0))
        except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
            return

        # TODO: we could use this code for the other check also
        goal_point = PointStamped()
        goal_point.header.frame_id = "/world"
        goal_point.header.stamp = rospy.Time(0)
        goal_point.point.x = self.nav_goal.position.x
        goal_point.point.y = self.nav_goal.position.y
        goal_point.point.z = self.nav_goal.position.z

        #print("Checking if nav goal is reached!")

        start_pos = np.array(trans)
        end_pos = np.array([self.nav_goal.position.x, self.nav_goal.position.y, self.nav_goal.position.z])
        if np.linalg.norm(start_pos - end_pos) < self.goal_tolerance:
            rospy.loginfo("Reached goal!")
            self.nav_goal = None

    def __init__(self, name):
        
        """Publish yaw setpoints based on waypoints"""
        self._action_name = name
        
        #self.heading_offset = rospy.get_param('~heading_offsets', 5.)
        self.goal_tolerance = rospy.get_param('~goal_tolerance', 5.)
        self.base_frame = rospy.get_param('~base_frame', "sam/base_link")

        self.nav_goal = None

        self.listener = tf.TransformListener()
        rospy.Timer(rospy.Duration(2), self.timer_callback)

        self.rpm_pub = rospy.Publisher('/sam/core/rpm_cmd', ThrusterRPMs, queue_size=10)
        self.yaw_pub = rospy.Publisher('/sam/ctrl/dynamic_heading/setpoint', Float64, queue_size=10)
        self.yaw_pid_enable = rospy.Publisher('/sam/ctrl/dynamic_heading/pid_enable', Bool, queue_size=10)
        self._as = actionlib.SimpleActionServer(self._action_name, MoveBaseAction, execute_cb=self.execute_cb, auto_start = False)
        self._as.start()
        rospy.loginfo("Announced action server with name: %s", self._action_name)

        rospy.spin()

if __name__ == '__main__':

    rospy.init_node('yaw_planner')
    planner = YawPlanner(rospy.get_name())