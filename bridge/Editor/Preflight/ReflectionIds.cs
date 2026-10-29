// SPDX-License-Identifier: Apache-2.0
using System;
using System.Reflection;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>
    /// The one derivation of type and field ids. Discovery rows carry these ids, and the IL write
    /// scan outside Unity (<c>tools/ilscan</c>) compiles this file so that its write sets name the
    /// same declarations with the same ids. It uses only <c>System.Reflection</c>, so it gives the
    /// same answer for live Editor types and for types loaded for inspection only.
    /// </summary>
    public static class ReflectionIds
    {
        public static string TypeId(Type type) => type.Assembly.GetName().Name + ":" + (type.FullName ?? type.Name);

        public static string FieldId(FieldInfo field) => TypeId(field.DeclaringType) + ":" + field.Name;
    }
}
