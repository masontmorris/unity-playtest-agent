// SPDX-License-Identifier: Apache-2.0
using System;
using UnityEngine;

namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// The one entity the session configured as controlled, and the pose a physics body measures
    /// for it. Generic over 2D and 3D physics and a transform-only object, and name-blind: the
    /// entity is whatever path <c>UPA_CONTROLLED_ENTITY</c> names, never a hard-coded object.
    ///
    /// The reader and the placement verb resolve the entity through this one place, so the pose an
    /// observation publishes and the body a placement moves are always the same object. Every
    /// member here runs on the Unity main thread.
    /// </summary>
    public static class UnityControlledEntity
    {
        /// <summary>The session configuration variable naming the controlled entity's hierarchy path.</summary>
        public const string VariableName = "UPA_CONTROLLED_ENTITY";

        /// <summary>A pose read from the transform: no body means no measured velocity.</summary>
        public const string SourceTransform = "transform";

        /// <summary>A 3D physics body: both the pose and the velocity are the body's.</summary>
        public const string SourceRigidbody = "rigidbody";

        /// <summary>A 2D physics body: the pose is planar and its velocity has no z component.</summary>
        public const string SourceRigidbody2D = "rigidbody2d";

        /// <summary>A character controller: the transform's pose, no body velocity.</summary>
        public const string SourceCharacterController = "character_controller";

        /// <summary>The configured hierarchy path, or null when the session configured none.</summary>
        public static string ConfiguredPath()
        {
            return Environment.GetEnvironmentVariable(VariableName);
        }

        /// <summary>
        /// Resolves the configured entity. An unconfigured or unresolvable entity yields a reason
        /// for the reader error, never a substitute object and never a pose of zero.
        /// </summary>
        public static bool TryResolve(out GameObject entity, out string reason)
        {
            entity = null;
            string path = ConfiguredPath();
            if (string.IsNullOrEmpty(path))
            {
                reason = VariableName + " is not configured";
                return false;
            }

            entity = GameObject.Find(path);
            if (entity == null)
            {
                reason = "controlled entity path not found: " + path;
                return false;
            }

            reason = null;
            return true;
        }

        /// <summary>
        /// Which measured source this entity's pose comes from, and its velocity in world units per
        /// second. A transform-only or character-controlled entity has no measured velocity; that
        /// stays null rather than being dressed up as zero.
        /// </summary>
        public static string ReadSource(GameObject entity, out Vector3? velocity)
        {
            if (entity == null)
            {
                velocity = null;
                return SourceTransform;
            }

            Rigidbody2D body2d = entity.GetComponent<Rigidbody2D>();
            if (body2d != null)
            {
                Vector2 planar = body2d.linearVelocity;
                velocity = new Vector3(planar.x, planar.y, 0f);
                return SourceRigidbody2D;
            }

            Rigidbody body = entity.GetComponent<Rigidbody>();
            if (body != null)
            {
                velocity = body.linearVelocity;
                return SourceRigidbody;
            }

            velocity = null;
            return entity.GetComponent<CharacterController>() != null ? SourceCharacterController : SourceTransform;
        }
    }
}
