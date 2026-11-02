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
    /// Reads the same probe through Unity's 2D physics, for an anchor that moves in a plane. A cast
    /// keeps x and y and drops z, which is all a planar world has; the vertical part of every probe
    /// ray is still the world's up, so the downward casts, the grid and the two blocker heights mean
    /// exactly what they mean in 3D. Colliders of the anchor and of its descendants are skipped the
    /// same way, and triggers are skipped by inspecting the collider rather than by changing a global
    /// query setting.
    /// </summary>
    public sealed class Physics2DProbeRaycasts : IProbeRaycasts
    {
        /// <summary>Most colliders one cast or overlap can consider.</summary>
        public const int MaxColliders = 32;

        private readonly Transform _anchor;

        private readonly RaycastHit2D[] _rayHits = new RaycastHit2D[MaxColliders];

        private readonly Collider2D[] _overlaps = new Collider2D[MaxColliders];

        public Physics2DProbeRaycasts(Transform anchor)
        {
            _anchor = anchor;
        }

        public bool Raycast(Vector3 origin, Vector3 direction, float maxDistance, out ProbeRayHit hit)
        {
            hit = default(ProbeRayHit);
            var from = new Vector2(origin.x, origin.y);
            var toward = new Vector2(direction.x, direction.y);
            if (toward.sqrMagnitude <= 0f)
            {
                return false;
            }

            int count = Physics2D.RaycastNonAlloc(
                from,
                toward.normalized,
                _rayHits,
                maxDistance,
                Physics2D.DefaultRaycastLayers);
            ProbeRayHit nearest = default(ProbeRayHit);
            ProbeRayHit nearestSelf = default(ProbeRayHit);
            bool found = false;
            bool foundSelf = false;
            for (int i = 0; i < count; i++)
            {
                RaycastHit2D candidate = _rayHits[i];
                if (candidate.collider == null || candidate.collider.isTrigger)
                {
                    continue;
                }

                var reading = new ProbeRayHit
                {
                    Hit = true,
                    // A planar collider has no extent along z, so the point reported is the one in the
                    // plane the cast was taken in: it keeps the cast's z rather than a made-up one.
                    Point = new Vector3(candidate.point.x, candidate.point.y, origin.z),
                    Normal = new Vector3(candidate.normal.x, candidate.normal.y, 0f),
                    Distance = Vector2.Distance(from, candidate.point),
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
            int count = Physics2D.OverlapCircleNonAlloc(
                new Vector2(point.x, point.y),
                radius,
                _overlaps,
                Physics2D.DefaultRaycastLayers);
            for (int i = 0; i < count; i++)
            {
                Collider2D candidate = _overlaps[i];
                if (candidate == null || candidate.isTrigger || IsSelf(candidate.transform))
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
    /// each point, three more rows at <c>-0.6</c>, <c>-1.2</c> and <c>-1.8</c> give the same for the
    /// floor behind, and two forward casts at fixed heights give the distance to the first
    /// obstruction in front. Nothing here is summarised: a point, a height, a normal and a
    /// distance are raw facts, and a reading that could not be taken is null, never zero.
    ///
    /// The same shape is published for a planar anchor, cast through 2D physics: ahead is the
    /// anchor's facing, the vertical axis is still the world's up, and the lateral offsets collapse
    /// into the plane, so only the in-plane row of each offset is cast and published. The
    /// <c>basis</c> field says which of the two was taken, which world direction the samples call
    /// ahead, where that heading came from, and whether the lateral axis was sampled.
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
    /// The anchor is resolved once and cached: <c>UPA_PROBE_ANCHOR</c> as a hierarchy path (else the
    /// session's configured <c>UPA_CONTROLLED_ENTITY</c>), then the first enabled
    /// <see cref="CharacterController"/>, then the first enabled <see cref="Rigidbody"/>, then the
    /// first <see cref="Rigidbody2D"/>. With none of them the probe is not published and the reason
    /// is reported as a reader error.
    /// </summary>
    public sealed class UnityProbeField
    {
        /// <summary>Environment variable naming the anchor as a hierarchy path.</summary>
        public const string AnchorVariable = "UPA_PROBE_ANCHOR";

        /// <summary>Why no probe is published when the scene offers no anchor.</summary>
        public const string NoAnchorReason =
            "no anchor: set UPA_PROBE_ANCHOR or have an enabled CharacterController, Rigidbody or Rigidbody2D in the scene";

        public const string KindPath = "path";

        public const string KindCharacterController = "character_controller";

        public const string KindRigidbody = "rigidbody";

        public const string KindRigidbody2D = "rigidbody2d";

        /// <summary>
        /// Shape version of the published probe. Version 2 adds the <c>basis</c> frame and the
        /// <c>backward</c> row of down-rays behind the anchor; every version 1 key keeps its meaning.
        /// </summary>
        public const int Version = 2;

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

        /// <summary>
        /// Offsets behind the anchor of the backward row, outermost loop first: the same distances as
        /// the forward grid, so a run-up can see where the floor it starts on ends.
        /// </summary>
        public static readonly float[] BackwardOffsets = { -0.6f, -1.2f, -1.8f };

        /// <summary>Lateral offsets of the sample grid, innermost loop first.</summary>
        public static readonly float[] LateralOffsets = { -0.35f, 0f, 0.35f };

        /// <summary>Heights of the two forward casts.</summary>
        public static readonly float[] BlockerHeights = { 0.15f, 0.9f };

        private readonly IProbeRaycasts _injected;

        private IProbeRaycasts _anchorRays;

        private Transform _anchorRaysFor;

        private bool _anchorRaysPlanar;

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
            if (string.IsNullOrEmpty(path)) path = Environment.GetEnvironmentVariable("UPA_CONTROLLED_ENTITY");
            Transform pathAnchor = string.IsNullOrEmpty(path) ? null : FindByPath(path);
            CharacterController controller = FirstEnabledController();
            Rigidbody body = FirstActiveBody();
            Rigidbody2D body2d = UnityEngine.Object.FindFirstObjectByType<Rigidbody2D>();
            bool found = ChooseAnchor(
                path,
                pathAnchor,
                controller == null ? null : controller.transform,
                body != null ? body.transform : (body2d == null ? null : body2d.transform),
                out anchor,
                out kind,
                out reason);
            if (found && string.IsNullOrEmpty(path) && controller == null && body == null && body2d != null)
                kind = KindRigidbody2D;
            return found;
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
        /// Samples the rows and the blockers around the cached anchor, through 3D or 2D physics
        /// depending on what the anchor is. Returns false with the reason when there is no anchor; any
        /// failure from the seam is thrown to the caller.
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
        /// Reads the rows and the blockers around <paramref name="anchor"/>, in the anchor's own
        /// frame: its position, the direction it calls ahead, and the lateral axis when the world has
        /// one. The published <c>basis</c> names that frame.
        /// </summary>
        public Dictionary<string, object> Sample(Transform anchor, string kind)
        {
            if (anchor == null)
            {
                throw new ArgumentNullException(nameof(anchor));
            }

            IProbeRaycasts rays = RaycastsFor(anchor);
            bool planar = IsPlanar(anchor);
            Vector3 origin = anchor.position;
            Vector3 forwardDir = Forward(anchor, planar);
            // A planar world has no lateral axis: there is nothing to cast across, so the lateral
            // offsets collapse into the one ray in the plane and only its reading is published.
            Vector3 rightDir = planar ? Vector3.zero : Horizontal(anchor.right, Vector3.right);
            ProbeSurface under = ReadSurface(rays, origin);
            double? groundY = under.Hit && !under.Inside ? under.Point.y : (double?)null;

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
                { "basis", Basis(forwardDir, planar) },
                { "under", Entry(0f, 0f, under, Relative(under, origin.y)) },
                { "samples", Row(rays, origin, forwardDir, rightDir, ForwardOffsets, groundY) },
                { "backward", Row(rays, origin, forwardDir, rightDir, BackwardOffsets, groundY) },
                { "blockers", blockers },
            };
        }

        /// <summary>
        /// One row of downward samples at the given offsets ahead of (or behind) the anchor, each
        /// measured against the ground under the anchor. A planar probe casts the single in-plane ray
        /// of each offset, so its row is one entry per offset rather than one per lateral step.
        /// </summary>
        private static List<object> Row(
            IProbeRaycasts rays,
            Vector3 origin,
            Vector3 forwardDir,
            Vector3 rightDir,
            float[] offsets,
            double? groundY)
        {
            bool lateral = rightDir.sqrMagnitude > MinimumHorizontalSqr;
            var entries = new List<object>(offsets.Length * (lateral ? LateralOffsets.Length : 1));
            for (int forwardIndex = 0; forwardIndex < offsets.Length; forwardIndex++)
            {
                if (!lateral)
                {
                    Vector3 straight = origin + forwardDir * offsets[forwardIndex];
                    ProbeSurface surface = ReadSurface(rays, straight);
                    entries.Add(Entry(offsets[forwardIndex], 0f, surface, Relative(surface, groundY)));
                    continue;
                }

                for (int lateralIndex = 0; lateralIndex < LateralOffsets.Length; lateralIndex++)
                {
                    Vector3 point = origin
                        + forwardDir * offsets[forwardIndex]
                        + rightDir * LateralOffsets[lateralIndex];
                    ProbeSurface surface = ReadSurface(rays, point);
                    entries.Add(Entry(
                        offsets[forwardIndex],
                        LateralOffsets[lateralIndex],
                        surface,
                        Relative(surface, groundY)));
                }
            }

            return entries;
        }

        /// <summary>
        /// The frame the samples were taken in, published so a reading never has to be guessed at: the
        /// plane, the world direction the samples call ahead, where that heading came from, and whether
        /// the lateral axis was sampled (<c>grid</c>) or collapsed into the plane (<c>collapsed</c>).
        /// </summary>
        private static Dictionary<string, object> Basis(Vector3 forwardDir, bool planar)
        {
            return new Dictionary<string, object>
            {
                { "plane", planar ? "2d" : "3d" },
                { "forward", ToArray(forwardDir) },
                { "forward_source", planar ? "facing_scale" : "transform_forward" },
                { "lateral", planar ? "collapsed" : "grid" },
            };
        }

        /// <summary>
        /// The heading the probe calls ahead. In 3D it is the transform's forward with the vertical part
        /// removed. In a plane it is the transform's own right axis with the plane's third axis removed,
        /// flipped by the sign of <c>localScale.x</c>, which is how a planar body publishes the way it
        /// faces (a body mirrored on x faces the other way with the same rotation).
        /// </summary>
        public static Vector3 Forward(Transform anchor, bool planar)
        {
            if (anchor == null)
            {
                return Vector3.forward;
            }

            if (!planar)
            {
                return Horizontal(anchor.forward, Vector3.forward);
            }

            var facing = new Vector3(anchor.right.x, anchor.right.y, 0f);
            if (facing.sqrMagnitude <= MinimumHorizontalSqr)
            {
                facing = Vector3.right;
            }

            facing = facing.normalized;
            return anchor.localScale.x < 0f ? -facing : facing;
        }

        /// <summary>
        /// Whether this anchor is measured in a plane. A 2D body on the anchor makes it planar, and so
        /// does the configured controlled entity carrying one, since that is the body the session moves.
        /// A 3D body on the anchor always wins: its own physics is what the readings must come from.
        /// </summary>
        public static bool IsPlanar(Transform anchor)
        {
            if (anchor == null)
            {
                return false;
            }

            if (anchor.GetComponent<Rigidbody>() != null)
            {
                return false;
            }

            if (anchor.GetComponent<Rigidbody2D>() != null || anchor.GetComponentInChildren<Rigidbody2D>() != null)
            {
                return true;
            }

            string path = UnityControlledEntity.ConfiguredPath();
            GameObject entity = string.IsNullOrEmpty(path) ? null : GameObject.Find(path);
            if (entity == null || entity == anchor.gameObject)
            {
                return false;
            }

            if (entity.GetComponent<Rigidbody>() != null)
            {
                return false;
            }

            return entity.GetComponent<Rigidbody2D>() != null
                || entity.GetComponentInChildren<Rigidbody2D>() != null;
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

            bool planar = IsPlanar(anchor);
            if (_anchorRays == null || _anchorRaysFor != anchor || _anchorRaysPlanar != planar)
            {
                _anchorRays = planar
                    ? (IProbeRaycasts)new Physics2DProbeRaycasts(anchor)
                    : new PhysicsProbeRaycasts(anchor);
                _anchorRaysFor = anchor;
                _anchorRaysPlanar = planar;
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
