// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using Playtest.Bridge.Protocol;
using UnityEngine;

namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// Which camera is the game view, and where it points. The frame capture composites the cameras
    /// that draw to the screen, lowest depth first; the game view is the first of them: the base
    /// layer of the picture, and the camera whose size sets the frame's size. The view reading
    /// comes from that same camera, so the angles describe the frame the capture writes. Both are
    /// engine facts: nothing here names a game object, a control or a genre.
    ///
    /// A reading is the direction the camera's forward vector points, yaw and pitch together. When
    /// there is no such camera, or the camera looks straight along the vertical axis where a
    /// heading does not exist, the view is unknown and <see cref="TryRead"/> says why; an unknown
    /// view is never reported as a heading of zero.
    /// </summary>
    public static class UnityGameView
    {
        /// <summary>
        /// Below this horizontal share of the forward vector (about 0.06 degrees from straight up or
        /// down) the heading is dominated by float noise and the view is reported as unknown.
        /// </summary>
        public const double MinimumHorizontalForward = 1e-3;

        /// <summary>
        /// The cameras that draw to the screen, in the order the capture composites them: lowest
        /// depth first, and an overlay camera (one that clears nothing) after a base camera of equal
        /// depth.
        /// </summary>
        public static List<Camera> ScreenCameras()
        {
            var cameras = new List<Camera>();
            foreach (Camera camera in Camera.allCameras)
            {
                if (camera == null || !camera.enabled || !camera.isActiveAndEnabled)
                {
                    continue;
                }

                if (camera.gameObject == null || !camera.gameObject.activeInHierarchy)
                {
                    continue;
                }

                if (camera.cameraType != CameraType.Game || camera.targetTexture != null)
                {
                    continue;
                }

                cameras.Add(camera);
            }

            cameras.Sort(CompareCameras);
            return cameras;
        }

        /// <summary>The game-view camera: the first screen camera, or null when none draws to the screen.</summary>
        public static Camera BaseCamera()
        {
            foreach (Camera camera in ScreenCameras())
            {
                return camera;
            }

            return null;
        }

        /// <summary>
        /// Reads the game view. Returns false with the reason when it is unknown; <paramref name="view"/>
        /// is then null.
        /// </summary>
        public static bool TryRead(out ObservationView view, out string reason)
        {
            Camera camera = BaseCamera();
            if (camera == null)
            {
                view = null;
                reason = "no game-view camera";
                return false;
            }

            Vector3 forward = camera.transform.forward;
            return TryFromForward(forward.x, forward.y, forward.z, out view, out reason);
        }

        /// <summary>
        /// Yaw and pitch of a forward vector, in degrees: yaw = atan2(x, z) and pitch = asin(-y / |forward|),
        /// the convention Unity's euler angles use. Returns false with the reason for a vector with no
        /// direction or no heading.
        /// </summary>
        public static bool TryFromForward(double x, double y, double z, out ObservationView view, out string reason)
        {
            view = null;
            double length = Math.Sqrt(x * x + y * y + z * z);
            if (double.IsNaN(length) || double.IsInfinity(length) || length <= 0)
            {
                reason = "the game-view camera has no forward direction";
                return false;
            }

            double horizontal = Math.Sqrt(x * x + z * z) / length;
            if (horizontal < MinimumHorizontalForward)
            {
                reason = "the game-view camera looks straight up or down, where its heading is undefined";
                return false;
            }

            double yaw = Math.Atan2(x, z) * 180.0 / Math.PI;
            double pitch = Math.Asin(Math.Max(-1.0, Math.Min(1.0, -y / length))) * 180.0 / Math.PI;
            view = new ObservationView
            {
                YawDeg = Math.Max(-ObservationView.MaxYawDeg, Math.Min(ObservationView.MaxYawDeg, yaw)),
                PitchDeg = Math.Max(-ObservationView.MaxPitchDeg, Math.Min(ObservationView.MaxPitchDeg, pitch)),
            };
            reason = null;
            return true;
        }

        private static int CompareCameras(Camera left, Camera right)
        {
            int byDepth = left.depth.CompareTo(right.depth);
            if (byDepth != 0)
            {
                return byDepth;
            }

            int leftOverlay = left.clearFlags == CameraClearFlags.Nothing ? 1 : 0;
            int rightOverlay = right.clearFlags == CameraClearFlags.Nothing ? 1 : 0;
            return leftOverlay.CompareTo(rightOverlay);
        }
    }
}
