// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using UnityEngine;

namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// One reading from the physics seam. <see cref="Hit"/> mirrors the seam's return value;
    /// <see cref="Self"/> marks a collider that belongs to the anchor or one of its descendants,
    /// which the probe treats as a miss. <see cref="Point"/>, <see cref="Normal"/> and
    /// <see cref="Distance"/> are only meaningful when a collider was struck.
    /// </summary>
    public struct ProbeRayHit
    {
        public bool Hit;

        public Vector3 Point;

        public Vector3 Normal;

        public float Distance;

        public bool Self;
    }

    /// <summary>
    /// The physics seam the probe reads through: one ray and one overlap sphere. Triggers are
    /// ignored by the physics implementation. Tests inject a fake so the grid, the arithmetic and
    /// the published shape can be checked without a physics scene.
    /// </summary>
    public interface IProbeRaycasts
    {
        /// <summary>
        /// Casts one ray. Returns true when a collider was struck; <paramref name="hit"/> then
        /// carries the nearest reading, marked self when nothing but the anchor's own colliders
        /// was struck.
        /// </summary>
        bool Raycast(Vector3 origin, Vector3 direction, float maxDistance, out ProbeRayHit hit);

        /// <summary>True when a collider other than the anchor's own overlaps a sphere.</summary>
        bool Overlap(Vector3 point, float radius);
    }

    /// <summary>
    /// Reads the probe through Unity's 3D physics. Colliders of the anchor and of its descendants
    /// are skipped so the anchor never reports itself; when only those are struck the nearest one
    /// comes back marked <see cref="ProbeRayHit.Self"/> so the probe can treat it as a miss.
    /// Triggers are ignored and the ordinary raycast layer mask applies, so both the cast and the
    /// overlap sphere see the same set of surfaces.
    /// </summary>
    public sealed class PhysicsProbeRaycasts : IProbeRaycasts
    {
        /// <summary>Most colliders one cast or overlap can consider.</summary>
        public const int MaxColliders = 32;

        private readonly Transform _anchor;

        private readonly RaycastHit[] _rayHits = new RaycastHit[MaxColliders];

        private readonly Collider[] _overlaps = new Collider[MaxColliders];

        public PhysicsProbeRaycasts(Transform anchor)
        {
            _anchor = anchor;
        }

        public bool Raycast(Vector3 origin, Vector3 direction, float maxDistance, out ProbeRayHit hit)
        {
            hit = default(ProbeRayHit);
            int count = Physics.RaycastNonAlloc(
                origin,
                direction,
                _rayHits,
                maxDistance,
                Physics.DefaultRaycastLayers,
                QueryTriggerInteraction.Ignore);

            bool found = false;
            bool foundSelf = false;
            ProbeRayHit nearest = default(ProbeRayHit);
            ProbeRayHit nearestSelf = default(ProbeRayHit);
            for (int i = 0; i < count; i++)
            {
                RaycastHit candidate = _rayHits[i];
                if (candidate.collider == null)
                {
                    continue;
                }

                var reading = new ProbeRayHit
                {
                    Hit = true,
                    Point = candidate.point,
                    Normal = candidate.normal,
                    Distance = candidate.distance,
                    Self = IsSelf(candidate.collider.transform),
                };

                if (reading.Self)
                {
                    if (!foundSelf || reading.Distance < nearestSelf.Distance)
                    {
                        foundSelf = true;
                        nearestSelf = reading;
                    }
                }
                else if (!found || reading.Distance < nearest.Distance)
                {
                    found = true;
                    nearest = reading;
                }
            }

            if (found)
            {
                hit = nearest;
                return true;
            }

            if (foundSelf)
            {
                hit = nearestSelf;
                return true;
            }

            return false;
        }

        public bool Overlap(Vector3 point, float radius)
        {
            int count = Physics.OverlapSphereNonAlloc(
                point,
                radius,
                _overlaps,
                Physics.DefaultRaycastLayers,
                QueryTriggerInteraction.Ignore);
            for (int i = 0; i < count; i++)
            {
                Collider candidate = _overlaps[i];
                if (candidate == null || IsSelf(candidate.transform))
                {
                    continue;
                }

                return true;
            }

            return false;
        }

        private bool IsSelf(Transform transform)
        {
            if (_anchor == null || transform == null)
            {
                return false;
            }

            return transform == _anchor || transform.IsChildOf(_anchor);
        }
    }

    /// <summary>
    /// The measured geometry a decision-maker reads under <c>public.probe</c>: the anchor the
    /// probe was taken from and the surfaces around it, in the anchor's own frame. Nine downward
    /// samples on a 3x3 grid of forward and lateral offsets give the height of whatever is under
    /// each point, and two forward casts at fixed heights give the distance to the first
    /// obstruction in front. Nothing here is summarised: a point, a height, a normal and a
    /// distance are raw facts, and a reading that could not be taken is null, never zero.
    ///
    /// Every grid <c>delta_y</c> is measured against the ground under the anchor (<c>under</c>), so
    /// flat ground is 0 and a surface one riser up is that riser's height, which is what a step
    /// limit can be compared with. <c>under</c> carries its own <c>delta_y</c> against the anchor,
    /// so it also says how far the anchor stands above its own floor. When the ground under the
    /// anchor is unknown, every grid <c>delta_y</c> is null rather than a height above nothing.
    ///
    /// A downward sample reports three outcomes. A surface under the point is a hit with its
    /// point, normal and <c>delta_y</c> (the hit height minus the anchor's height); a point with
    /// no surface under it is a miss with every reading null; a point inside geometry is a hit
    /// with the readings null and <c>inside</c> true, which is the only reading an obstruction
    /// taller than the cast or an overhang can give.
    ///
    /// The anchor is resolved once and cached: <c>UPA_PROBE_ANCHOR</c> as a hierarchy path, then
    /// the first enabled <see cref="CharacterController"/>, then the first enabled
    /// <see cref="Rigidbody"/>. With none of them the probe is not published and the reason is
    /// reported as a reader error.
    /// </summary>
    public sealed class UnityProbeField
    {
        /// <summary>Environment variable naming the anchor as a hierarchy path.</summary>
        public const string AnchorVariable = "UPA_PROBE_ANCHOR";

        /// <summary>Why no probe is published when the scene offers no anchor.</summary>
        public const string NoAnchorReason =
            "no anchor: set UPA_PROBE_ANCHOR or have an enabled CharacterController or Rigidbody in the scene";

        public const string KindPath = "path";

        public const string KindCharacterController = "character_controller";

        public const string KindRigidbody = "rigidbody";

        /// <summary>Shape version of the published probe.</summary>
        public const int Version = 1;

        /// <summary>Height above each sample point the downward cast starts at.</summary>
        public const float CastHeight = 6f;

        /// <summary>Length of the downward cast.</summary>
        public const float CastDistance = 12f;

        /// <summary>Radius of the overlap sphere that detects a point inside geometry.</summary>
        public const float OverlapRadius = 0.05f;

        /// <summary>Length of each forward cast toward a blocker.</summary>
        public const float BlockerMaxDistance = 1.8f;

        /// <summary>Below this squared horizontal length a direction has no heading to report.</summary>
        public const float MinimumHorizontalSqr = 1e-12f;

        /// <summary>Forward offsets of the sample grid, outermost loop first.</summary>
        public static readonly float[] ForwardOffsets = { 0.6f, 1.2f, 1.8f };

        /// <summary>Lateral offsets of the sample grid, innermost loop first.</summary>
        public static readonly float[] LateralOffsets = { -0.35f, 0f, 0.35f };

        /// <summary>Heights of the two forward casts.</summary>
        public static readonly float[] BlockerHeights = { 0.15f, 0.9f };

        private readonly IProbeRaycasts _injected;

        private IProbeRaycasts _anchorRays;

        private Transform _anchorRaysFor;

        private Transform _anchor;

        private string _anchorKind;

        /// <summary>
        /// Creates a probe reader. <paramref name="raycasts"/> replaces the physics seam, which is
        /// what tests use; null reads through <see cref="PhysicsProbeRaycasts"/> for the resolved
        /// anchor.
        /// </summary>
        public UnityProbeField(IProbeRaycasts raycasts = null)
        {
            _injected = raycasts;
        }

        /// <summary>The kind of the cached anchor, or null before one is resolved.</summary>
        public string AnchorKind
        {
            get { return _anchorKind; }
        }

        /// <summary>
        /// The cached anchor, resolving one when none is cached or the cached reference was
        /// destroyed. Returns false with the reason when there is no anchor.
        /// </summary>
        public bool TryResolve(out Transform anchor, out string reason)
        {
            if (_anchor != null)
            {
                anchor = _anchor;
                reason = null;
                return true;
            }

            _anchor = null;
            _anchorKind = null;

            string kind;
            if (!TryFindAnchor(out anchor, out kind, out reason))
            {
                anchor = null;
                return false;
            }

            _anchor = anchor;
            _anchorKind = kind;
            reason = null;
            return true;
        }

        /// <summary>
        /// Resolves an anchor from <c>UPA_PROBE_ANCHOR</c> as a hierarchy path, then the first
        /// enabled <see cref="CharacterController"/>, then the first enabled
        /// <see cref="Rigidbody"/>. A Rigidbody has no enabled flag of its own, so an active one is
        /// its candidate.
        /// </summary>
        public static bool TryFindAnchor(out Transform anchor, out string kind, out string reason)
        {
            string path = Environment.GetEnvironmentVariable(AnchorVariable);
            Transform pathAnchor = string.IsNullOrEmpty(path) ? null : FindByPath(path);
            CharacterController controller = FirstEnabledController();
            Rigidbody body = FirstActiveBody();
            return ChooseAnchor(
                path,
                pathAnchor,
                controller == null ? null : controller.transform,
                body == null ? null : body.transform,
                out anchor,
                out kind,
                out reason);
        }

        /// <summary>
        /// The resolution order itself, with the scene already searched: the environment path when it
        /// is set, then the controller, then the body, else the documented reason. A path that names
        /// nothing does not fall back to a component, because that would quietly measure an object the
        /// caller did not ask for. Pure, so the order and the reason are testable without a scene.
        /// </summary>
        public static bool ChooseAnchor(
            string path,
            Transform pathAnchor,
            Transform controller,
            Transform body,
            out Transform anchor,
            out string kind,
            out string reason)
        {
            anchor = null;
            kind = null;

            if (!string.IsNullOrEmpty(path))
            {
                if (pathAnchor == null)
                {
                    reason = "the " + AnchorVariable + " path names no GameObject in the loaded scenes: " + path;
                    return false;
                }

                anchor = pathAnchor;
                kind = KindPath;
                reason = null;
                return true;
            }

            if (controller != null)
            {
                anchor = controller;
                kind = KindCharacterController;
                reason = null;
                return true;
            }

            if (body != null)
            {
                anchor = body;
                kind = KindRigidbody;
                reason = null;
                return true;
            }

            reason = NoAnchorReason;
            return false;
        }

        private static Transform FindByPath(string path)
        {
            GameObject found = GameObject.Find(path);
            return found == null ? null : found.transform;
        }

        /// <summary>
        /// Samples the grid and the blockers around the cached anchor. Returns false with the
        /// reason when there is no anchor; any failure from the seam is thrown to the caller.
        /// </summary>
        public bool TrySample(out Dictionary<string, object> probe, out string reason)
        {
            probe = null;
            Transform anchor;
            if (!TryResolve(out anchor, out reason))
            {
                return false;
            }

            probe = Sample(anchor, _anchorKind);
            reason = null;
            return true;
        }

        /// <summary>
        /// Reads the grid and the blockers around <paramref name="anchor"/>, in the anchor's own
        /// frame: its position, its forward and right directions with the vertical part removed.
        /// </summary>
        public Dictionary<string, object> Sample(Transform anchor, string kind)
        {
            if (anchor == null)
            {
                throw new ArgumentNullException(nameof(anchor));
            }

            IProbeRaycasts rays = RaycastsFor(anchor);
            Vector3 origin = anchor.position;
            Vector3 forwardDir = Horizontal(anchor.forward, Vector3.forward);
            Vector3 rightDir = Horizontal(anchor.right, Vector3.right);
            ProbeSurface under = ReadSurface(rays, origin);
            double? groundY = under.Hit && !under.Inside ? under.Point.y : (double?)null;

            var samples = new List<object>(ForwardOffsets.Length * LateralOffsets.Length);
            for (int forwardIndex = 0; forwardIndex < ForwardOffsets.Length; forwardIndex++)
            {
                for (int lateralIndex = 0; lateralIndex < LateralOffsets.Length; lateralIndex++)
                {
                    Vector3 point = origin
                        + forwardDir * ForwardOffsets[forwardIndex]
                        + rightDir * LateralOffsets[lateralIndex];
                    ProbeSurface surface = ReadSurface(rays, point);
                    samples.Add(Entry(
                        ForwardOffsets[forwardIndex],
                        LateralOffsets[lateralIndex],
                        surface,
                        Relative(surface, groundY)));
                }
            }

            var blockers = new List<object>(BlockerHeights.Length);
            for (int i = 0; i < BlockerHeights.Length; i++)
            {
                blockers.Add(SampleBlocker(rays, origin, forwardDir, BlockerHeights[i]));
            }

            return new Dictionary<string, object>
            {
                { "version", Version },
                {
                    "anchor",
                    new Dictionary<string, object>
                    {
                        { "kind", kind },
                        { "name", anchor.gameObject.name },
                        { "position", ToArray(origin) },
                    }
                },
                { "under", Entry(0f, 0f, under, Relative(under, origin.y)) },
                { "samples", samples },
                { "blockers", blockers },
            };
        }

        /// <summary>
        /// A direction with its vertical part removed and normalized, or the fallback when the
        /// horizontal part is too small to have a heading (looking straight up or down).
        /// </summary>
        public static Vector3 Horizontal(Vector3 direction, Vector3 fallback)
        {
            var flat = new Vector3(direction.x, 0f, direction.z);
            if (flat.sqrMagnitude <= MinimumHorizontalSqr)
            {
                return fallback;
            }

            return flat.normalized;
        }

        private IProbeRaycasts RaycastsFor(Transform anchor)
        {
            if (_injected != null)
            {
                return _injected;
            }

            if (_anchorRays == null || _anchorRaysFor != anchor)
            {
                _anchorRays = new PhysicsProbeRaycasts(anchor);
                _anchorRaysFor = anchor;
            }

            return _anchorRays;
        }

        private static ProbeSurface ReadSurface(IProbeRaycasts rays, Vector3 point)
        {
            ProbeRayHit reading;
            bool struck = rays.Raycast(
                point + Vector3.up * CastHeight,
                Vector3.down,
                CastDistance,
                out reading);
            if (struck && !reading.Self)
            {
                var surface = new ProbeSurface { Hit = true, Point = reading.Point, Normal = reading.Normal };
                return surface;
            }

            if (rays.Overlap(point, OverlapRadius))
            {
                return new ProbeSurface { Hit = true, Inside = true };
            }

            return ProbeSurface.None;
        }

        /// <summary>
        /// The height of one surface above a reference height. Unknown when the surface could not be
        /// measured or the reference is unknown, which is what keeps a missing reading from becoming a
        /// difference of zero.
        /// </summary>
        private static double? Relative(ProbeSurface surface, double? referenceY)
        {
            if (!surface.Hit || surface.Inside || !referenceY.HasValue)
            {
                return null;
            }

            return surface.Point.y - referenceY.Value;
        }

        private static Dictionary<string, object> SampleBlocker(
            IProbeRaycasts rays,
            Vector3 origin,
            Vector3 forwardDir,
            float height)
        {
            ProbeRayHit reading;
            bool struck = rays.Raycast(
                origin + Vector3.up * height,
                forwardDir,
                BlockerMaxDistance,
                out reading);
            bool hit = struck && !reading.Self;

            object distance = hit ? (object)(double)reading.Distance : null;
            object point = hit ? (object)ToArray(reading.Point) : null;
            object normal = hit ? (object)ToArray(reading.Normal) : null;

            return new Dictionary<string, object>
            {
                { "height", height },
                { "max_distance", BlockerMaxDistance },
                { "hit", hit },
                { "distance", distance },
                { "point", point },
                { "normal", normal },
            };
        }

        private static Dictionary<string, object> Entry(
            float forward,
            float lateral,
            ProbeSurface surface,
            double? deltaY)
        {
            bool readable = surface.Hit && !surface.Inside;
            return new Dictionary<string, object>
            {
                { "forward", forward },
                { "lateral", lateral },
                { "hit", surface.Hit },
                { "point", readable ? (object)ToArray(surface.Point) : null },
                { "delta_y", deltaY.HasValue ? (object)deltaY.Value : null },
                { "normal", readable ? (object)ToArray(surface.Normal) : null },
                { "inside", surface.Inside },
            };
        }

        /// <summary>What one downward sample found: a readable surface, a point inside geometry, or nothing.</summary>
        private struct ProbeSurface
        {
            public static readonly ProbeSurface None = default(ProbeSurface);

            public bool Hit;

            public bool Inside;

            public Vector3 Point;

            public Vector3 Normal;
        }

        private static List<object> ToArray(Vector3 value)
        {
            return new List<object> { value.x, value.y, value.z };
        }

        private static CharacterController FirstEnabledController()
        {
            CharacterController[] found = UnityEngine.Object.FindObjectsByType<CharacterController>(
                FindObjectsInactive.Exclude);
            for (int i = 0; i < found.Length; i++)
            {
                CharacterController candidate = found[i];
                if (candidate != null && candidate.enabled)
                {
                    return candidate;
                }
            }

            return null;
        }

        private static Rigidbody FirstActiveBody()
        {
            Rigidbody[] found = UnityEngine.Object.FindObjectsByType<Rigidbody>(
                FindObjectsInactive.Exclude);
            for (int i = 0; i < found.Length; i++)
            {
                Rigidbody candidate = found[i];
                if (candidate != null)
                {
                    return candidate;
                }
            }

            return null;
        }
    }
}
