// SPDX-License-Identifier: Apache-2.0
using System;
using UnityEngine.SceneManagement;

namespace Playtest.Bridge.Editor
{
    public static class BuildSceneSelection
    {
        public static string Resolve()
        {
            string requested = Environment.GetEnvironmentVariable("UPA_SCENE_PATH");
            if (string.IsNullOrEmpty(requested)) requested = SceneUtility.GetScenePathByBuildIndex(0);
            for (int i = 0; i < SceneManager.sceneCountInBuildSettings; i++)
                if (string.Equals(SceneUtility.GetScenePathByBuildIndex(i), requested, StringComparison.Ordinal))
                    return requested;
            throw new InvalidOperationException("Scene is not an enabled build scene: " + requested);
        }
    }
}
