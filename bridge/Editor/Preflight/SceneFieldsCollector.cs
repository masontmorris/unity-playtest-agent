using System;
using UnityEngine;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>
    /// One bounded sample of the live scene's GameObjects and fields, for repeated freshness
    /// measurement. It reuses <see cref="SceneDiscoveryCollector"/> with the caller's limits, so
    /// object and field ids join a discovery snapshot exactly and every row keeps the same
    /// coverage rules. Only <c>game_objects</c> and <c>fields</c> are carried, plus the instant
    /// they were sampled. The reused collector still reads its ordinary scene and binding data;
    /// source excerpts, IL and binding rows are not included in this response.
    ///
    /// It does not enter Play mode, load a scene, write to the project or generate input. Stage
    /// checks belong to the caller.
    /// </summary>
    public static class SceneFieldsCollector
    {
        public static InspectionDocument Collect(int maxComponents, int maxFields, int maxBindings)
        {
            if (maxComponents <= 0) throw new ArgumentOutOfRangeException(nameof(maxComponents));
            if (maxFields <= 0) throw new ArgumentOutOfRangeException(nameof(maxFields));
            if (maxBindings <= 0) throw new ArgumentOutOfRangeException(nameof(maxBindings));

            var document = new InspectionDocument();
            document.Data["time_unit"] = "seconds";
            document.Data["frame"] = null;
            document.Data["real_time_s"] = null;
            document.Data["game_time_s"] = null;
            document.Data["sample_wall_s"] = null;
            document.Data["keyboard_state"] = SceneKeyboardState.Snapshot();
            SampleInstant(document);

            SceneDiscoveryResult discovery =
                SceneDiscoveryCollector.Collect(maxComponents, maxFields, maxBindings);

            document.Data["game_objects"] = discovery.Data["game_objects"];
            document.Data["fields"] = discovery.Data["fields"];
            CopyCap(document, discovery, "game_objects", SceneDiscoveryCollector.ObjectLimit);
            CopyCap(document, discovery, "fields", maxFields);
            document.Errors.AddRange(discovery.Errors);
            return document;
        }

        private static void SampleInstant(InspectionDocument document)
        {
            try
            {
                document.Data["frame"] = Time.frameCount;
            }
            catch (Exception ex)
            {
                document.Errors.Add("frame: " + Describe(ex));
            }

            try
            {
                document.Data["real_time_s"] = Time.realtimeSinceStartupAsDouble;
            }
            catch (Exception ex)
            {
                document.Errors.Add("real_time_s: " + Describe(ex));
            }

            try
            {
                document.Data["game_time_s"] = Time.timeAsDouble;
            }
            catch (Exception ex)
            {
                document.Errors.Add("game_time_s: " + Describe(ex));
            }

            try
            {
                document.Data["sample_wall_s"] =
                    (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
            }
            catch (Exception ex)
            {
                document.Errors.Add("sample_wall_s: " + Describe(ex));
            }

        }

        private static void CopyCap(InspectionDocument document, SceneDiscoveryResult discovery, string name, int limit)
        {
            if (!discovery.Caps.TryGetValue(name, out SceneCap cap))
            {
                document.Caps[name] = new InspectionCap
                {
                    Limit = limit,
                    Observed = null,
                    Returned = 0,
                    Truncated = null,
                };
                document.Errors.Add(name + ": coverage unavailable");
                return;
            }

            document.Caps[name] = new InspectionCap
            {
                Limit = cap.Limit,
                Observed = cap.Observed,
                Returned = cap.Returned,
                Truncated = cap.Truncated,
            };
        }

        private static string Describe(Exception exception)
        {
            if (exception == null) return "unknown";
            return exception.GetType().Name + ": " + exception.Message;
        }
    }
}
