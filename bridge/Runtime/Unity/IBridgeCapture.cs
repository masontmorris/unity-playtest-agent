// SPDX-License-Identifier: Apache-2.0
namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// Offscreen frame capture. Available only after the startup self-test proves that a camera
    /// and an overlay canvas can be rendered into a render target and encoded.
    /// </summary>
    public interface IBridgeCapture
    {
        bool Available { get; }

        /// <summary>Absolute path to the latest complete JPEG, or null before the first frame.</summary>
        string LatestFrameRef { get; }
    }
}
