using System;
using System.Collections.Generic;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using UnityEditor;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>
    /// Supplies unranked authored source excerpts. It does not guess which script causes motion;
    /// callers can inspect all returned text and the cap report tells them what was omitted.
    /// </summary>
    public static class SourceCollector
    {
        public const int FileLimit = 20000;
        public const int PerFileCharacterLimit = 32768;
        public const int TotalCharacterLimit = 750000;

        public static InspectionDocument Collect()
        {
            var document = new InspectionDocument();
            document.Data["source_selection"] = "unranked_authored_scripts";
            var excerpts = new List<object>();
            document.Data["source_excerpts"] = excerpts;
            try
            {
                List<string> paths = AssetDatabase.FindAssets("t:MonoScript")
                    .Select(AssetDatabase.GUIDToAssetPath)
                    .Where(path => !string.IsNullOrEmpty(path) && ProjectSurveyCollector.IsAuthoredAsset(path))
                    .Distinct(StringComparer.Ordinal)
                    .OrderBy(path => path, StringComparer.Ordinal).ToList();
                List<string> selected = document.Take("source_files", paths, FileLimit);
                int returned = 0;
                long observed = 0;
                bool incomplete = selected.Count != paths.Count;
                foreach (string path in selected)
                {
                    var item = new Dictionary<string, object> { ["path"] = path };
                    excerpts.Add(item);
                    try
                    {
                        MonoScript script = AssetDatabase.LoadAssetAtPath<MonoScript>(path);
                        if (script == null || script.text == null)
                            throw new InvalidOperationException("source unavailable");
                        string source = script.text;
                        int take = Math.Min(source.Length, Math.Min(PerFileCharacterLimit, TotalCharacterLimit - returned));
                        observed += source.Length;
                        item["text"] = source.Substring(0, take);
                        item["sha256"] = Hash(source);
                        item["character_count"] = source.Length;
                        item["returned_characters"] = take;
                        item["truncated"] = take < source.Length;
                        document.Caps["source_file:" + path] = new InspectionCap
                        {
                            Limit = PerFileCharacterLimit,
                            Observed = source.Length,
                            Returned = take,
                            Truncated = take < source.Length,
                        };
                        returned += take;
                    }
                    catch (Exception ex)
                    {
                        incomplete = true;
                        item["text"] = null;
                        item["sha256"] = null;
                        item["character_count"] = null;
                        item["returned_characters"] = 0;
                        item["truncated"] = null;
                        document.Unavailable("source_file:" + path, PerFileCharacterLimit, ex.GetType().Name);
                    }
                }
                document.Caps["source_characters"] = new InspectionCap
                {
                    Limit = TotalCharacterLimit,
                    Observed = incomplete || observed > int.MaxValue ? (int?)null : (int)observed,
                    Returned = returned,
                    Truncated = observed > returned ? true : incomplete ? (bool?)null : false,
                };
            }
            catch (Exception ex)
            {
                document.Data["source_excerpts"] = null;
                document.Unavailable("source_files", FileLimit, ex.GetType().Name);
                document.Unavailable("source_characters", TotalCharacterLimit, ex.GetType().Name);
            }
            return document;
        }

        private static string Hash(string text)
        {
            using (SHA256 sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(text))).Replace("-", "").ToLowerInvariant();
        }
    }
}
