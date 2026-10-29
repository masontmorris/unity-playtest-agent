// SPDX-License-Identifier: Apache-2.0
using UnityEngine;

namespace Playtest.Bridge.Tests
{
    public sealed class SceneVectorProbe : MonoBehaviour
    {
        public Vector2 Planar = new Vector2(1.5f, -2.25f);
        public Vector3 Velocity = new Vector3(1f, 2f, 3f);
        public Vector4 Tint = new Vector4(0.25f, 0.5f, 0.75f, 1f);
        public Quaternion Facing = new Quaternion(0f, 0.5f, 0f, 0.5f);
        public Vector2Int Cell = new Vector2Int(3, -4);
        public Vector3Int Voxel = new Vector3Int(5, 6, -7);
        public Vector3 Broken = new Vector3(float.NaN, 1f, float.PositiveInfinity);
        public Color Paint = Color.red;
    }
}
