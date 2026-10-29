// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.InputSystem;

namespace Playtest.Bridge.Tests
{
    public sealed class SceneDiscoveryProbe : SceneDiscoveryProbeBase
    {
        public int Number = 7;
        public string Label = "probe";
        public UnityEngine.Object NullReference;
        public UnityEngine.Object DestroyedReference;
        [NonSerialized] public Dictionary<string, string> Unsupported = new Dictionary<string, string>();
        [SerializeField] private int _serializedPrivate = 11;
        [NonSerialized] public int NonSerializedPublic = 13;
        [HideInInspector] public int Hidden = 17;
        [SerializeField] private InputAction _inlineAction;

        public InputAction InlineAction
        {
            get { return _inlineAction; }
        }

        public void SetInlineAction(InputAction action)
        {
            _inlineAction = action;
        }

        public void SetSerializedPrivate(int value)
        {
            _serializedPrivate = value;
        }
    }
}
