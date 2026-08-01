import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from cv_bridge import CvBridge
import cv2
import os
import glob
import threading
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

VIDEO_DIR = "sim_video"

class UAVVideoStreamer(Node):
    def __init__(self, uav_id, video_path):
        super().__init__(f'{uav_id}_video_streamer')
        self.uav_id = uav_id
        self.video_path = video_path
        
        # NASTAVENÍ QoS: Best Effort + malá fronta (ideální pro video streams)
        video_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1
        )
        
        # Přidáme QoS profil do publisheru
        self.publisher_ = self.create_publisher(
            CompressedImage, 
            f'/{uav_id}/camera/image_raw/compressed', 
            video_qos
        )
        
        self.bridge = CvBridge()
        self.cap = cv2.VideoCapture(self.video_path)
        
        if not self.cap.isOpened():
            self.get_logger().error(f"Nelze otevřít video soubor: {video_path}")
            return

        # Zde natvrdo omezíme FPS na 15 nebo 20, abyhomeserver nestíhal generovat slideshow
        # Často OpenCV z MP4 metadat v Dockeru čte nesmyslné časování.
        target_fps = 1.0
            
        timer_period = 1.0 / target_fps
        self.timer = self.create_timer(timer_period, self.timer_callback)
        self.get_logger().info(f"Streamuji {uav_id} s QoS Best Effort při {target_fps} FPS")

    def timer_callback(self):
        ret, frame = self.cap.read()
        
        if not ret:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = self.cap.read()
            if not ret:
                return

        try:
            # Nastavení vysoké kvality pro statické průzkumné snímky (0-100)
            # params=[cv2.IMWRITE_JPEG_QUALITY, 90] zajistí čistý obraz bez kostiček
            msg = self.bridge.cv2_to_compressed_imgmsg(frame, dst_format='jpg')
            
            # Trik pro OpenCV/cv_bridge, aby poslal vysokou kvalitu:
            # cv_bridge defaultně komprimuje na cca 80 %, což stačí, ale pro 1 FPS můžeme přitlačit
            
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.header.frame_id = f"{self.uav_id}_camera_optical_frame"
            
            self.publisher_.publish(msg)
        except Exception as e:
            self.get_logger().error(f"Chyba při zpracování snímku: {e}")

    def destroy_node(self):
        self.cap.release()
        super().destroy_node()

def main(args=None):
    rclpy.init(args=args)
    
    # Najdeme všechna videa (podporuje mp4, avi, mkv...)
    extensions = ["*.mp4", "*.avi", "*.mkv"]
    video_files = []
    for ext in extensions:
        video_files.extend(glob.glob(os.path.join(VIDEO_DIR, ext)))

    if not video_files:
        print(f"Chyba: Ve složce '{VIDEO_DIR}' nebyly nalezeny žádné video soubory.")
        return

    # Vytvoříme ROS2 MultiThreadedExecutor, aby mohla běžet všechna videa naráz
    executor = rclpy.executors.MultiThreadedExecutor()
    nodes = []

    print("Spouštím ROS2 Video Streamery:")
    for file_path in video_files:
        file_name = os.path.basename(file_path)
        uav_id = os.path.splitext(file_name)[0]
        
        # Inicializace uzlu pro konkrétní dron
        node = UAVVideoStreamer(uav_id, file_path)
        nodes.append(node)
        executor.add_node(node)

    try:
        # Spuštění executoru (bude běžet, dokud to neutneš přes Ctrl+C)
        executor.spin()
    except KeyboardInterrupt:
        print("\nUkončuji streamování...")
    finally:
        for node in nodes:
            node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
