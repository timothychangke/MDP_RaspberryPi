#!/usr/bin/env python3
import json
import io
import picamera
import queue
import time
from multiprocessing import Process, Manager
from typing import Optional
import os
import requests
from communication.android import AndroidLink, AndroidMessage
from communication.stm32 import STMLink
from consts import SYMBOL_MAP
from logger import prepare_logger
from settings import API_IP, API_PORT


class PiAction:
    """
    Class that represents an action that the RPi needs to take.    
    """

    def __init__(self, cat, value):
        """
        :param cat: The category of the action. Can be 'info', 'mode', 'path', 'snap', 'obstacle', 'location', 'failed', 'success'
        :param value: The value of the action. Can be a string, a list of coordinates, or a list of obstacles.
        """
        self._cat = cat
        self._value = value

    @property
    def cat(self):
        return self._cat

    @property
    def value(self):
        return self._value


class RaspberryPi:
    """
    Class that represents the Raspberry Pi.
    """

    def __init__(self):
        """
        Initializes the Raspberry Pi.
        """
        self.logger = prepare_logger()
        self.android_link = AndroidLink()
        self.stm_link = STMLink()

        self.manager = Manager()

        self.android_dropped = self.manager.Event()
        self.unpause = self.manager.Event()

        self.movement_lock = self.manager.Lock()

        self.android_queue = self.manager.Queue()  # Messages to send to Android
        # Messages that need to be processed by RPi
        self.rpi_action_queue = self.manager.Queue()
        # Messages that need to be processed by STM32, as well as snap commands
        self.command_queue = self.manager.Queue()
        # X,Y,D coordinates of the robot after execution of a command
        self.path_queue = self.manager.Queue()

        self.proc_recv_android = None
        self.proc_recv_stm32 = None
        self.proc_android_sender = None
        self.proc_command_follower = None
        self.proc_rpi_action = None
        self.rs_flag = False
        self.success_obstacles = self.manager.list()
        self.failed_obstacles = self.manager.list()
        self.obstacles = self.manager.dict()
        self.current_location = self.manager.dict()
        self.failed_attempt = False

    def start(self):
        """Starts the RPi orchestrator"""
        try:
            ### Start up initialization ###

            #self.android_link.connect()
            #self.android_queue.put(AndroidMessage(
                #'info', 'You are connected to the RPi!'))
            #self.stm_link.connect()
            self.check_api()

            # Define child processes
            #self.proc_recv_android = Process(target=self.recv_android)
            #self.proc_recv_stm32 = Process(target=self.recv_stm)
            #self.proc_android_sender = Process(target=self.android_sender)
            # self.proc_command_follower = Process(target=self.command_follower)
            self.proc_rpi_action = Process(target=self.rpi_action)

            # Start child processes
            #self.proc_recv_android.start()
            #self.proc_recv_stm32.start()
            #self.proc_android_sender.start()
            # self.proc_command_follower.start()
            # self.proc_rpi_action.start()
            self.snap_and_rec("1_C")

            self.logger.info("Child Processes started")

            ### Start up complete ###

            # Send success message to Android
            #self.android_queue.put(AndroidMessage('info', 'Robot is ready!'))
            #self.android_queue.put(AndroidMessage('mode', 'path'))
            #self.reconnect_android()

        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        """Stops all processes on the RPi and disconnects gracefully with Android and STM32"""
        self.android_link.disconnect()
        self.stm_link.disconnect()
        self.logger.info("Program exited!")

    def reconnect_android(self):
        """Handles the reconnection to Android in the event of a lost connection."""
        self.logger.info("Reconnection handler is watching...")

        while True:
            # Wait for android connection to drop
            self.android_dropped.wait()

            self.logger.error("Android link is down!")

            # Kill child processes
            self.logger.debug("Killing android child processes")
            self.proc_android_sender.kill()
            self.proc_recv_android.kill()

            # Wait for the child processes to finish
            self.proc_android_sender.join()
            self.proc_recv_android.join()
            assert self.proc_android_sender.is_alive() is False
            assert self.proc_recv_android.is_alive() is False
            self.logger.debug("Android child processes killed")

            # Clean up old sockets
            self.android_link.disconnect()

            # Reconnect
            self.android_link.connect()

            # Recreate Android processes
            self.proc_recv_android = Process(target=self.recv_android)
            self.proc_android_sender = Process(target=self.android_sender)

            # Start previously killed processes
            self.proc_recv_android.start()
            self.proc_android_sender.start()

            self.logger.info("Android child processes restarted")
            self.android_queue.put(AndroidMessage(
                "info", "You are reconnected!"))
            self.android_queue.put(AndroidMessage('mode', 'path'))

            self.android_dropped.clear()

    def recv_android(self) -> None:
        """
        [Child Process] Processes the messages received from Android
        """
        while True:
            msg_str: Optional[str] = None
            try:
                msg_str = self.android_link.recv()
            except OSError:
                self.android_dropped.set()
                self.logger.debug("Event set: Android connection dropped")

            if msg_str is None:
                continue
            
            message: dict = json.loads(msg_str)

            ## Command: Set obstacles ##
            if message["cat"] == "obstacles":
                self.rpi_action_queue.put(PiAction(**message))
                self.logger.debug(f"Set obstacles PiAction added to queue: {message}")

            ## Command: Start Moving ##
            elif message["cat"] == "control":
                if message["value"] == "start":
                    self.rpi_action_queue.put(PiAction(**message))
                    self.logger.debug(
                        f"Control start PiAction added to queue: {message}"
                    )

    def recv_stm(self) -> None:
        """
        [Child Process] Receive acknowledgement messages from STM32, and release the movement lock
        """
        while True:

            message: str = self.stm_link.recv()

            if message.startswith("ACK"):
                if self.rs_flag == False:
                    self.rs_flag = True
                    self.logger.debug("ACK for RS00 from STM32 received.")
                    continue
                try:
                    self.movement_lock.release()
                    try:
                        self.retrylock.release()
                    except:
                        pass
                    self.logger.debug(
                        "ACK from STM32 received, movement lock released.")

                    cur_location = self.path_queue.get_nowait()

                    self.current_location['x'] = cur_location['x']
                    self.current_location['y'] = cur_location['y']
                    self.current_location['d'] = cur_location['d']
                    self.logger.info(
                        f"self.current_location = {self.current_location}")
                    self.android_queue.put(AndroidMessage('location', {
                        "x": cur_location['x'],
                        "y": cur_location['y'],
                        "d": cur_location['d'],
                    }))

                except Exception:
                    self.logger.warning("Tried to release a released lock!")
            else:
                self.logger.warning(
                    f"Ignored unknown message from STM: {message}")

    def android_sender(self) -> None:
        """
        [Child process] Responsible for retrieving messages from android_queue and sending them over the Android link. 
        """
        while True:
            # Retrieve from queue
            try:
                message: AndroidMessage = self.android_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self.android_link.send(message)
            except OSError:
                self.android_dropped.set()
                self.logger.debug("Event set: Android dropped")

    def command_follower(self) -> None:
        """
        [Child Process] 
        """
        while True:
            # Retrieve next movement command
            command: str = self.command_queue.get()
            self.logger.debug("wait for unpause")
            # Wait for unpause event to be true [Main Trigger]
            try:
                self.logger.debug("wait for retrylock")
                self.retrylock.acquire()
                self.retrylock.release()
            except:
                self.logger.debug("wait for unpause")
                self.unpause.wait()
            self.logger.debug("wait for movelock")
            # Acquire lock first (needed for both moving, and snapping pictures)
            self.movement_lock.acquire()

            # STM32 Commands - Send straight to STM32
            stm32_prefixes = ("FS", "BS", "FW", "BW", "FL", "FR", "BL",
                              "BR", "TL", "TR", "A", "C", "DT", "STOP", "ZZ", "RS")
            if command.startswith(stm32_prefixes):
                self.stm_link.send(command)
                self.logger.debug(f"Sending to STM32: {command}")

            # Snap command
            elif command.startswith("SNAP"):
                obstacle_id_with_signal = command.replace("SNAP", "")

                self.rpi_action_queue.put(
                    PiAction(cat="snap", value=obstacle_id_with_signal))

            # End of path
            elif command == "FIN":
                self.logger.info(
                    f"At FIN, self.failed_obstacles: {self.failed_obstacles}")
                self.logger.info(
                    f"At FIN, self.current_location: {self.current_location}")
                if len(self.failed_obstacles) != 0 and self.failed_attempt == False:

                    new_obstacle_list = list(self.failed_obstacles)
                    for i in list(self.success_obstacles):
                        # {'x': 5, 'y': 11, 'id': 1, 'd': 4}
                        i['d'] = 8
                        new_obstacle_list.append(i)

                    self.logger.info("Attempting to go to failed obstacles")
                    self.failed_attempt = True
                    self.request_algo({'obstacles': new_obstacle_list, 'mode': '0'},
                                      self.current_location['x'], self.current_location['y'], self.current_location['d'], retrying=True)
                    self.retrylock = self.manager.Lock()
                    self.movement_lock.release()
                    continue

                self.unpause.clear()
                self.movement_lock.release()
                self.logger.info("Commands queue finished.")
                self.android_queue.put(AndroidMessage(
                    "info", "Commands queue finished."))
                self.android_queue.put(AndroidMessage("status", "finished"))
                self.rpi_action_queue.put(PiAction(cat="stitch", value=""))
            else:
                raise Exception(f"Unknown command: {command}")

    def rpi_action(self):
        """
        [Child Process]
        """
        while True:
            action: PiAction = self.rpi_action_queue.get()
            self.logger.debug(
                f"PiAction retrieved from queue: {action.cat} {action.value}"
            )

            if action.cat == "obstacles":
                for obs in action.value["obstacles"]:
                    self.obstacles[obs["id"]] = obs
                self.request_algo(action.value)
            elif action.cat == "snap":
                self.snap_and_rec(obstacle_id_with_signal=action.value)
            elif action.cat == "stitch":
                self.request_stitch()
            elif action.cat == "control" and action.value == "start":
                # Check API
                if not self.check_api():
                    self.logger.error("API is down! Start command aborted.")
                    self.android_queue.put(
                        AndroidMessage("error", "API is down, start command aborted.")
                    )

                # Commencing path following
                if not self.command_queue.empty():
                    self.logger.info("Gryo reset!")
                    self.stm_link.send("RS00")
                    # Main trigger to start movement #
                    self.unpause.set()
                    self.logger.info("Start command received, starting robot on path!")
                    self.android_queue.put(
                        AndroidMessage("info", "Starting robot on path!")
                    )
                    self.android_queue.put(AndroidMessage("status", "running"))
                else:
                    self.logger.warning(
                        "The command queue is empty, please set obstacles."
                    )
                    self.android_queue.put(
                        AndroidMessage(
                            "error", "Command queue is empty, did you set obstacles?"
                        )
                    )

    def snap_and_rec(self, obstacle_id_with_signal: str) -> None:
        """
        RPi snaps an image and calls the API for image-rec.
        The response is then forwarded back to the android
        :param obstacle_id: the current obstacle ID followed by underscore followed by signal
        
        ##############
        Code to capture image and store temporarily before sending to image-rec API endpoint via http
        ##############
        """

        # notify android
        obstacle_id, signal = obstacle_id_with_signal.split("_")
        self.logger.info(f"Capturing image for obstacle id: {obstacle_id_with_signal}")
        # have to change obstacle_id to obstacle_id
        #self.android_queue.put(AndroidMessage("info", f"Capturing image for obstacle id: {obstacle_id}"))

        #capture an image
        stream = io.BytesIO()
        with picamera.PiCamera() as camera:
            camera.start_preview()
            camera.vflip = True  # Vertical flip
            camera.hflip = True
            time.sleep(1)
            camera.capture(stream,format='jpeg')

        # notify android
        #self.android_queue.put(AndroidMessage("info", "Image captured. Calling image-rec api..."))
        self.logger.info("Image captured. Calling image-rec api...")

        # call image-rec API endpoint
        self.logger.debug("Requesting from image API")
        url = f"http://{API_IP}:{API_PORT}/image"
        filename = f"{int(time.time())}_{obstacle_id}_{signal}.jpg"
        image_data = stream.getvalue()
        response = requests.post(url, files={"file": (filename, image_data)})
        

        if response.status_code != 200:
            self.logger.error("Something went wrong when requesting path from image-rec API. Please try again.")
            #self.android_queue.put(AndroidMessage(
                #"error", "Something went wrong when requesting path from image-rec API. Please try again."))
            return
        
        results = json.loads(response.content)

        # release lock so that bot can continue moving
        #self.movement_lock.release()
        #try:
        #    self.retrylock.release()
        #except:
        #    pass

        self.logger.info(f"results: {results}")
        self.logger.info(f"self.obstacles: {self.obstacles}")
        self.logger.info(
            f"Image recognition results: {results} ({SYMBOL_MAP.get(results['image_id'])})")

        if results['image_id'] == 'NA':
            self.failed_obstacles.append(
                self.obstacles[int(results['obstacle_id'])])
            self.logger.info(
                f"Added Obstacle {results['obstacle_id']} to failed obstacles.")
            self.logger.info(f"self.failed_obstacles: {self.failed_obstacles}")
        else:
            self.success_obstacles.append(
                self.obstacles[int(results['obstacle_id'])])
            self.logger.info(
                f"self.success_obstacles: {self.success_obstacles}")
        #self.android_queue.put(AndroidMessage("image-rec", results))

    def request_algo(self, data, robot_x=1, robot_y=1, robot_dir=0, retrying=False):
        """
        Requests for a series of commands and the path from the Algo API.
        The received commands and path are then queued in the respective queues
        """
        self.logger.info("Requesting path from algo...")
        self.android_queue.put(AndroidMessage(
            "info", "Requesting path from algo..."))
        self.logger.info(f"data: {data}")
        body = {**data, "big_turn": "0", "robot_x": robot_x,
                "robot_y": robot_y, "robot_dir": robot_dir, "retrying": retrying}
        url = f"http://{API_IP}:{API_PORT}/compute"
        response = requests.post(url, json=body)

        # Error encountered at the server, return early
        if response.status_code != 200:
            self.android_queue.put(AndroidMessage(
                "error", "Something went wrong when requesting path from Algo API."))
            self.logger.error(
                "Something went wrong when requesting path from Algo API.")
            return

        # Parse response
        result = json.loads(response.content)['data']
        commands = result['commands']
        path = result['path']

        # Log commands received
        self.logger.debug(f"Commands received from API: {commands}")

        # Put commands and paths into respective queues
        self.clear_queues()
        for c in commands:
            self.command_queue.put(c)
        for p in path[1:]:  # ignore first element as it is the starting position of the robot
            self.path_queue.put(p)

        self.android_queue.put(AndroidMessage(
            "info", "Commands and path received Algo API. Robot is ready to move."))
        self.logger.info(
            "Commands and path received Algo API. Robot is ready to move.")

    def request_stitch(self):
        """Sends a stitch request to the image recognition API to stitch the different images together"""
        url = f"http://{API_IP}:{API_PORT}/stitch"
        response = requests.get(url)

        # If error, then log, and send error to Android
        if response.status_code != 200:
            # Notify android
            self.android_queue.put(AndroidMessage(
                "error", "Something went wrong when requesting stitch from the API."))
            self.logger.error(
                "Something went wrong when requesting stitch from the API.")
            return

        self.logger.info("Images stitched!")
        self.android_queue.put(AndroidMessage("info", "Images stitched!"))

    def clear_queues(self):
        """Clear both command and path queues"""
        while not self.command_queue.empty():
            self.command_queue.get()
        while not self.path_queue.empty():
            self.path_queue.get()

    def check_api(self) -> bool:
        """Check whether image recognition and algorithm API server is up and running

        Returns:
            bool: True if running, False if not.
        """
        print("hello")
        # Check image recognition API
        url = f"http://{API_IP}:{API_PORT}/"
        try:
            print('hi')
            response = requests.get(url, timeout=1)
            if response.status_code == 200:
                self.logger.debug("API is up!")
                # stream = io.BytesIO()
                # with picamera.PiCamera() as camera:
                #     camera.start_preview()
                #     time.sleep(1)
                #     camera.capture(stream,format='jpeg')

                # self.logger.info("Image capture. calling image-rec api...")

                # self.logger.debug("Requesting from image API")
                # url = f"http://{API_IP}:{API_PORT}/image"
                # filename = f"{int(time.time())}_{obstacle_id}.jpg"
                # print(filename)
                # image_data = stream.getvalue()
                # response = requests.post(url, files={"file": (filename, image_data)})   
                # #response = requests.post(url, files={"file": (filename, open(filename, 'rb'))})
                # print(response)

                # if response.status_code != 200:
                #     self.logger.error("Something went wrong ")
                #     return
                
                # results = json.loads(response.content)
                # #results = {'image_id': '10', 'obstacle_id': obstacle_id}
                # self.logger.info(f"results: {results}")
                # self.logger.info(f"self.obstacles: {self.obstacles}")
                # self.logger.info(
                #     f"Image recognition results: {results} ({SYMBOL_MAP.get(results['image_id'])})")

                # if results['image_id'] == 'NA':
                #     self.failed_obstacles.append(
                #         self.obstacles[results['obstacle_id']])
                #     self.logger.info(
                #         f"Added Obstacle {results['obstacle_id']} to failed obstacles.")
                #     self.logger.info(f"self.failed_obstacles: {self.failed_obstacles}")
                # else:
                #     self.success_obstacles.append(
                #         self.obstacles[int(results['obstacle_id'])])
                #     self.logger.info(
                #         f"self.success_obstacles: {self.success_obstacles}")
                return True
            return False
        
            

        # If error, then log, and return False
        except ConnectionError:
            self.logger.warning("API Connection Error")
            return False
        except requests.Timeout:
            self.logger.warning("API Timeout")
            return False
        except Exception as e:
            self.logger.warning(f"API Exception: {e}")
            return False


if __name__ == "__main__":
    rpi = RaspberryPi()
    rpi.start()
