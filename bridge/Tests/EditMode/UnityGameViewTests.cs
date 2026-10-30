// SPDX-License-Identifier: Apache-2.0
using NUnit.Framework;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEngine;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The game view: which camera it is, how its direction becomes yaw and pitch, and that an
    /// unknown view stays unknown. Pure direction math is checked on vectors; the camera path is
    /// checked on a camera drawn before every other, so the test does not depend on the scene.
    /// </summary>
    [TestFixture]
    public sealed class UnityGameViewTests
    {
        private const double AngleToleranceDeg = 0.001;

        private GameObject _cameraObject;

        [TearDown]
        public void TearDown()
        {
            if (_cameraObject != null)
            {
                Object.DestroyImmediate(_cameraObject);
                _cameraObject = null;
            }
        }

        [TestCase(0.0, 0.0, 1.0, 0.0, 0.0, TestName = "ahead")]
        [TestCase(1.0, 0.0, 0.0, 90.0, 0.0, TestName = "right")]
        [TestCase(-1.0, 0.0, 0.0, -90.0, 0.0, TestName = "left")]
        [TestCase(0.0, 0.0, -1.0, 180.0, 0.0, TestName = "behind")]
        [TestCase(0.0, -0.5, 0.8660254, 0.0, 30.0, TestName = "down thirty")]
        [TestCase(0.0, 0.5, 0.8660254, 0.0, -30.0, TestName = "up thirty")]
        [TestCase(0.0, -1.0, 2.0, 0.0, 26.565051, TestName = "forward need not be unit length")]
        public void Direction_BecomesYawAndPitchInUnityConvention(
            double x, double y, double z, double yaw, double pitch)
        {
            Assert.That(UnityGameView.TryFromForward(x, y, z, out ObservationView view, out string reason), Is.True, reason);

            Assert.That(reason, Is.Null);
            Assert.That(view.PitchDeg, Is.EqualTo(pitch).Within(AngleToleranceDeg));
            if (yaw == 180.0)
            {
                Assert.That(System.Math.Abs(view.YawDeg), Is.EqualTo(180.0).Within(AngleToleranceDeg), "behind is the seam");
            }
            else
            {
                Assert.That(view.YawDeg, Is.EqualTo(yaw).Within(AngleToleranceDeg));
            }
        }

        [TestCase(0.0, 0.0, 0.0, TestName = "no direction")]
        [TestCase(0.0, 1.0, 0.0, TestName = "straight up")]
        [TestCase(0.0, -1.0, 0.0, TestName = "straight down")]
        [TestCase(0.0004, -1.0, 0.0004, TestName = "past the pole")]
        [TestCase(double.NaN, 0.0, 1.0, TestName = "not a number")]
        [TestCase(double.PositiveInfinity, 0.0, 1.0, TestName = "infinite")]
        public void NoHeading_IsUnknownAndNeverZero(double x, double y, double z)
        {
            bool known = UnityGameView.TryFromForward(x, y, z, out ObservationView view, out string reason);

            Assert.That(known, Is.False);
            Assert.That(view, Is.Null, "an unknown view is null, not a heading of zero");
            Assert.That(reason, Is.Not.Null.And.Not.Empty);
        }

        [Test]
        public void Camera_ReportsItsWorldYawAndPitch()
        {
            Camera camera = CreateBaseCamera(Quaternion.Euler(20f, 135f, 0f));

            Assert.That(UnityGameView.BaseCamera(), Is.SameAs(camera));
            Assert.That(UnityGameView.TryRead(out ObservationView view, out string reason), Is.True, reason);

            Assert.That(view.YawDeg, Is.EqualTo(135.0).Within(0.01));
            Assert.That(view.PitchDeg, Is.EqualTo(20.0).Within(0.01), "looking down is a positive pitch");
        }

        [Test]
        public void Camera_YawWrapsAtTheSeamInsteadOfPassingThreeSixty()
        {
            // Unity's euler y of 350 is a heading ten degrees left of ahead: yaw is -10, not 350.
            CreateBaseCamera(Quaternion.Euler(0f, 350f, 0f));

            Assert.That(UnityGameView.TryRead(out ObservationView view, out string reason), Is.True, reason);

            Assert.That(view.YawDeg, Is.EqualTo(-10.0).Within(0.01));
        }

        [Test]
        public void Camera_RollDoesNotChangeTheHeading()
        {
            CreateBaseCamera(Quaternion.Euler(10f, 40f, 25f));

            Assert.That(UnityGameView.TryRead(out ObservationView view, out string reason), Is.True, reason);

            Assert.That(view.YawDeg, Is.EqualTo(40.0).Within(0.01));
            Assert.That(view.PitchDeg, Is.EqualTo(10.0).Within(0.01));
        }

        [Test]
        public void ObservationSource_PublishesTheViewBesideThePublicFactsNotInThem()
        {
            CreateBaseCamera(Quaternion.Euler(-15f, 60f, 0f));
            using (var source = new UnityObservationSource())
            {
                source.Start();

                BridgeObservationSnapshot snapshot = source.GetObservation();

                Assert.That(snapshot.View, Is.Not.Null, string.Join("; ", snapshot.ReaderErrors.ConvertAll(e => e.Reader + ": " + e.Error)));
                Assert.That(snapshot.View.YawDeg, Is.EqualTo(60.0).Within(0.01));
                Assert.That(snapshot.View.PitchDeg, Is.EqualTo(-15.0).Within(0.01), "looking up is a negative pitch");
                Assert.That(snapshot.Public.ContainsKey("view"), Is.False, "what a decision-maker sees under public must not change");
                Assert.That(snapshot.ReaderErrors.Exists(e => e.Reader == "view"), Is.False, "a known view has no reader error");
            }
        }

        private Camera CreateBaseCamera(Quaternion rotation)
        {
            _cameraObject = new GameObject("GameViewTestCamera");
            Camera camera = _cameraObject.AddComponent<Camera>();
            camera.enabled = true;
            camera.depth = -10000f;
            _cameraObject.transform.rotation = rotation;
            return camera;
        }
    }
}
