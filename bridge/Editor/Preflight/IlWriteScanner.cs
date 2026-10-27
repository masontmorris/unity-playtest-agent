using System;
using System.Collections.Generic;
using System.Reflection;
using System.Reflection.Emit;
using System.Text;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>
    /// Reads the IL of methods and constructors declared by the supplied types and records the field
    /// tokens they pass to <c>stfld</c> or <c>stsfld</c>.
    /// </summary>
    /// <remarks>
    /// This is a direct-write scan, not a semantic classification. It does not recurse into base
    /// types, interfaces or other assemblies, and it does not follow calls. A method that does not
    /// contain a direct store can still change a field through a helper, a property setter or an
    /// address taken by <c>ldflda</c>/<c>ldsflda</c>. Absence of a direct write is therefore never
    /// evidence that a field is configuration, read-only, or unused. Only
    /// <see cref="IlWriteScanResult.IsComplete"/> says that the direct-write evidence covers every
    /// method body considered by the scan.
    /// </remarks>
    public static class IlWriteScanner
    {
        private const BindingFlags DeclaredMembers =
            BindingFlags.Public
            | BindingFlags.NonPublic
            | BindingFlags.Instance
            | BindingFlags.Static
            | BindingFlags.DeclaredOnly;

        private const int Stfld = 0x7D;
        private const int Stsfld = 0x80;
        private const int Ldflda = 0x7C;
        private const int Ldsflda = 0x7F;

        private static readonly Dictionary<int, OpCode> OpCodeLookup = BuildOpCodeLookup();

        /// <summary>
        /// Scans the declared methods and constructors of <paramref name="types"/>.
        /// </summary>
        /// <param name="types">
        /// The types whose methods should be inspected. Only members declared by these exact types are
        /// scanned; base types and referenced assemblies are not traversed.
        /// </param>
        /// <param name="maxMethods">
        /// The maximum number of method bodies to inspect. Must be greater than zero. The result
        /// records whether the cap prevented the scan from covering every candidate.
        /// </param>
        /// <returns>
        /// Direct writes plus every reason the scan could not be exhaustive. A cap or any issue makes
        /// <see cref="IlWriteScanResult.IsComplete"/> false.
        /// </returns>
        /// <exception cref="ArgumentNullException"><paramref name="types"/> is null.</exception>
        /// <exception cref="ArgumentOutOfRangeException"><paramref name="maxMethods"/> is zero or negative.</exception>
        public static IlWriteScanResult Scan(IEnumerable<Type> types, int maxMethods)
        {
            if (types == null)
            {
                throw new ArgumentNullException(nameof(types));
            }

            if (maxMethods <= 0)
            {
                throw new ArgumentOutOfRangeException(
                    nameof(maxMethods),
                    maxMethods,
                    "maxMethods must be greater than zero; a scan with no method budget cannot claim completeness.");
            }

            var issues = new List<IlWriteScanIssue>();
            List<Type> orderedTypes = ReadTypes(types, issues);

            var candidates = new List<MethodBase>();
            for (int i = 0; i < orderedTypes.Count; i++)
            {
                CollectMethods(orderedTypes[i], candidates, issues);
            }

            var writes = new List<IlWrite>();
            int scanned = 0;
            for (int i = 0; i < candidates.Count; i++)
            {
                if (scanned >= maxMethods)
                {
                    break;
                }

                scanned++;
                ScanMethod(candidates[i], writes, issues);
            }

            bool limitReached = scanned < candidates.Count;
            return new IlWriteScanResult(
                writes.AsReadOnly(),
                issues.AsReadOnly(),
                candidates.Count,
                scanned,
                limitReached);
        }

        private static List<Type> ReadTypes(IEnumerable<Type> types, List<IlWriteScanIssue> issues)
        {
            var ordered = new List<Type>();
            var seen = new HashSet<Type>();
            IEnumerator<Type> enumerator = null;
            int index = 0;

            try
            {
                try
                {
                    enumerator = types.GetEnumerator();
                }
                catch (Exception ex)
                {
                    if (IsFatal(ex))
                    {
                        throw;
                    }

                    issues.Add(IlWriteScanIssue.ForType(
                        null,
                        IlWriteScanIssueKind.TypeEnumerationFailed,
                        "the provided type sequence could not be enumerated: " + DescribeException(ex) + "."));
                    return ordered;
                }

                while (true)
                {
                    Type current;
                    try
                    {
                        if (!enumerator.MoveNext())
                        {
                            break;
                        }

                        current = enumerator.Current;
                    }
                    catch (Exception ex)
                    {
                        if (IsFatal(ex))
                        {
                            throw;
                        }

                        issues.Add(IlWriteScanIssue.ForType(
                            null,
                            IlWriteScanIssueKind.TypeEnumerationFailed,
                            "enumerating the provided types failed at entry " + index + ": "
                            + DescribeException(ex) + "; the scan is partial."));
                        break;
                    }

                    if (current == null)
                    {
                        issues.Add(IlWriteScanIssue.ForType(
                            null,
                            IlWriteScanIssueKind.TypeEnumerationFailed,
                            "the provided type sequence contained a null entry at index " + index + "."));
                    }
                    else if (seen.Add(current))
                    {
                        ordered.Add(current);
                    }

                    index++;
                }
            }
            finally
            {
                if (enumerator != null)
                {
                    try
                    {
                        enumerator.Dispose();
                    }
                    catch (Exception)
                    {
                        // Disposal cannot change which methods were observed.
                    }
                }
            }

            ordered.Sort(CompareTypes);
            return ordered;
        }

        private static void CollectMethods(Type type, List<MethodBase> candidates, List<IlWriteScanIssue> issues)
        {
            var found = new List<MethodBase>();
            try
            {
                MemberInfo[] members = type.GetMembers(DeclaredMembers);
                for (int i = 0; i < members.Length; i++)
                {
                    MethodBase method = members[i] as MethodBase;
                    if (method == null || method.IsAbstract)
                    {
                        // Abstract methods cannot contain IL, so there is nothing to scan and no
                        // missing-body error to report.
                        continue;
                    }

                    found.Add(method);
                }
            }
            catch (Exception ex)
            {
                if (IsFatal(ex))
                {
                    throw;
                }

                issues.Add(IlWriteScanIssue.ForType(
                    type,
                    IlWriteScanIssueKind.TypeInaccessible,
                    "the declared members of type '" + DescribeType(type) + "' could not be enumerated: "
                    + DescribeException(ex) + "; writes in this type are unknown."));
                return;
            }

            found.Sort(CompareMethods);
            for (int i = 0; i < found.Count; i++)
            {
                if (i > 0 && CompareMethods(found[i - 1], found[i]) == 0)
                {
                    continue;
                }

                candidates.Add(found[i]);
            }
        }

        private static void ScanMethod(MethodBase method, List<IlWrite> writes, List<IlWriteScanIssue> issues)
        {
            MethodBody body;
            try
            {
                body = method.GetMethodBody();
            }
            catch (Exception ex)
            {
                if (IsFatal(ex))
                {
                    throw;
                }

                issues.Add(IlWriteScanIssue.ForMethod(
                    method,
                    IlWriteScanIssueKind.MissingMethodBody,
                    -1,
                    "the IL body of this non-abstract method could not be read: "
                    + DescribeException(ex) + "; its writes are unknown."));
                return;
            }

            if (body == null)
            {
                issues.Add(IlWriteScanIssue.ForMethod(
                    method,
                    IlWriteScanIssueKind.MissingMethodBody,
                    -1,
                    "no IL body is available for this non-abstract method; its writes are unknown."));
                return;
            }

            byte[] il;
            try
            {
                il = body.GetILAsByteArray();
            }
            catch (Exception ex)
            {
                if (IsFatal(ex))
                {
                    throw;
                }

                issues.Add(IlWriteScanIssue.ForMethod(
                    method,
                    IlWriteScanIssueKind.MissingMethodBody,
                    -1,
                    "the IL bytes of this non-abstract method could not be read: "
                    + DescribeException(ex) + "; its writes are unknown."));
                return;
            }

            if (il == null)
            {
                issues.Add(IlWriteScanIssue.ForMethod(
                    method,
                    IlWriteScanIssueKind.MissingMethodBody,
                    -1,
                    "the method body reported no IL byte array for this non-abstract method; its writes are unknown."));
                return;
            }

            Type[] typeArguments = null;
            Type[] methodArguments = null;
            try
            {
                Type declaringType = method.DeclaringType;
                if (declaringType != null && declaringType.IsGenericType)
                {
                    typeArguments = declaringType.GetGenericArguments();
                }

                if (method.IsGenericMethod)
                {
                    methodArguments = method.GetGenericArguments();
                }
            }
            catch (Exception ex)
            {
                if (IsFatal(ex))
                {
                    throw;
                }

                issues.Add(IlWriteScanIssue.ForMethod(
                    method,
                    IlWriteScanIssueKind.DecodeFailed,
                    -1,
                    "the generic context needed to resolve field tokens could not be read: "
                    + DescribeException(ex) + "; its writes are unknown."));
                return;
            }

            var seenFields = new HashSet<string>(StringComparer.Ordinal);
            int position = 0;
            while (position < il.Length)
            {
                int offset = position;
                int opcodeValue = il[position++];
                if (opcodeValue == 0xFE)
                {
                    if (position >= il.Length)
                    {
                        issues.Add(IlWriteScanIssue.ForMethod(
                            method,
                            IlWriteScanIssueKind.DecodeFailed,
                            offset,
                            "IL decode failed at offset " + FormatOffset(offset)
                            + ": the two-byte opcode prefix has no second byte."));
                        return;
                    }

                    opcodeValue = 0xFE00 | il[position++];
                }

                OpCode opcode;
                if (!OpCodeLookup.TryGetValue(opcodeValue, out opcode))
                {
                    issues.Add(IlWriteScanIssue.ForMethod(
                        method,
                        IlWriteScanIssueKind.DecodeFailed,
                        offset,
                        "IL decode failed at offset " + FormatOffset(offset)
                        + ": unknown opcode 0x" + opcodeValue.ToString("X4") + "."));
                    return;
                }

                int operandSize;
                string operandError;
                if (!TryGetOperandSize(opcode, il, position, out operandSize, out operandError))
                {
                    issues.Add(IlWriteScanIssue.ForMethod(
                        method,
                        IlWriteScanIssueKind.DecodeFailed,
                        offset,
                        "IL decode failed at offset " + FormatOffset(offset) + ": " + operandError));
                    return;
                }

                if (operandSize > il.Length - position)
                {
                    issues.Add(IlWriteScanIssue.ForMethod(
                        method,
                        IlWriteScanIssueKind.DecodeFailed,
                        offset,
                        "IL decode failed at offset " + FormatOffset(offset) + ": opcode '" + opcode.Name
                        + "' needs " + operandSize + " operand byte(s), but only "
                        + (il.Length - position) + " remain."));
                    return;
                }

                int operandOffset = position;
                position += operandSize;

                if (opcodeValue == Stfld || opcodeValue == Stsfld)
                {
                    int token = ReadInt32(il, operandOffset);
                    FieldInfo field;
                    string error;
                    if (TryResolveField(method.Module, token, typeArguments, methodArguments, out field, out error))
                    {
                        if (seenFields.Add(FieldKey(field)))
                        {
                            writes.Add(new IlWrite(field, method, offset));
                        }
                    }
                    else
                    {
                        issues.Add(IlWriteScanIssue.ForField(
                            method,
                            offset,
                            IlWriteScanIssueKind.UnresolvedFieldToken,
                            null,
                            "the field token 0x" + token.ToString("X8") + " passed to '" + opcode.Name
                            + "' at offset " + FormatOffset(offset) + " could not be resolved: " + error
                            + "; the write is unknown."));
                    }
                }
                else if (opcodeValue == Ldflda || opcodeValue == Ldsflda)
                {
                    int token = ReadInt32(il, operandOffset);
                    FieldInfo field;
                    string error;
                    bool resolved = TryResolveField(
                        method.Module,
                        token,
                        typeArguments,
                        methodArguments,
                        out field,
                        out error);
                    string target = resolved
                        ? "'" + DescribeField(field) + "'"
                        : "field token 0x" + token.ToString("X8") + " (" + error + ")";

                    issues.Add(IlWriteScanIssue.ForField(
                        method,
                        offset,
                        IlWriteScanIssueKind.IndirectWrite,
                        field,
                        "potential indirect write: '" + opcode.Name + "' at offset " + FormatOffset(offset)
                        + " addresses " + target
                        + "; a direct write scan does not follow stores through the address, so this method's effect on the field is unresolved."));
                }
            }
        }

        private static bool TryResolveField(
            Module module,
            int token,
            Type[] typeArguments,
            Type[] methodArguments,
            out FieldInfo field,
            out string error)
        {
            field = null;
            error = null;

            if (module == null)
            {
                error = "the method has no module";
                return false;
            }

            try
            {
                field = module.ResolveField(token, typeArguments, methodArguments);
                if (field == null)
                {
                    error = "ResolveField returned null";
                    return false;
                }

                return true;
            }
            catch (Exception ex)
            {
                if (IsFatal(ex))
                {
                    throw;
                }

                error = DescribeException(ex);
                return false;
            }
        }

        private static bool TryGetOperandSize(
            OpCode opcode,
            byte[] il,
            int position,
            out int size,
            out string error)
        {
            size = 0;
            error = null;

            switch (opcode.OperandType)
            {
                case OperandType.InlineNone:
                    return true;

                case OperandType.ShortInlineI:
                case OperandType.ShortInlineVar:
                case OperandType.ShortInlineBrTarget:
                    size = 1;
                    return true;

                case OperandType.InlineVar:
                    size = 2;
                    return true;

                case OperandType.InlineI:
                case OperandType.InlineBrTarget:
                case OperandType.InlineField:
                case OperandType.InlineMethod:
                case OperandType.InlineType:
                case OperandType.InlineString:
                case OperandType.InlineSig:
                case OperandType.InlineTok:
                case OperandType.ShortInlineR:
                    size = 4;
                    return true;

                case OperandType.InlineI8:
                case OperandType.InlineR:
                    size = 8;
                    return true;

                case OperandType.InlineSwitch:
                    if (position > il.Length - 4)
                    {
                        error = "the switch operand has no room for its target count.";
                        return false;
                    }

                    uint targetCount = unchecked((uint)ReadInt32(il, position));
                    long targetBytes = (long)targetCount * 4L;
                    int available = il.Length - position - 4;
                    if (targetBytes > available)
                    {
                        error = "the switch operand declares " + targetCount + " target(s), which needs "
                            + targetBytes + " byte(s), but only " + available + " remain.";
                        return false;
                    }

                    size = 4 + (int)targetBytes;
                    return true;

                default:
                    error = "opcode '" + opcode.Name + "' uses the unsupported operand type '"
                        + opcode.OperandType + "'.";
                    return false;
            }
        }

        private static Dictionary<int, OpCode> BuildOpCodeLookup()
        {
            var lookup = new Dictionary<int, OpCode>();
            FieldInfo[] fields = typeof(OpCodes).GetFields(BindingFlags.Public | BindingFlags.Static);
            for (int i = 0; i < fields.Length; i++)
            {
                FieldInfo field = fields[i];
                if (field.FieldType != typeof(OpCode))
                {
                    continue;
                }

                OpCode opcode = (OpCode)field.GetValue(null);
                lookup[((int)opcode.Value) & 0xFFFF] = opcode;
            }

            return lookup;
        }

        private static int ReadInt32(byte[] bytes, int offset)
        {
            return bytes[offset]
                | (bytes[offset + 1] << 8)
                | (bytes[offset + 2] << 16)
                | (bytes[offset + 3] << 24);
        }

        private static int CompareTypes(Type left, Type right)
        {
            int byName = string.CompareOrdinal(TypeKey(left), TypeKey(right));
            if (byName != 0)
            {
                return byName;
            }

            return SafeMetadataToken(left).CompareTo(SafeMetadataToken(right));
        }

        private static int CompareMethods(MethodBase left, MethodBase right)
        {
            int bySignature = string.CompareOrdinal(MethodKey(left), MethodKey(right));
            if (bySignature != 0)
            {
                return bySignature;
            }

            return SafeMetadataToken(left).CompareTo(SafeMetadataToken(right));
        }

        private static string TypeKey(Type type)
        {
            if (type == null)
            {
                return string.Empty;
            }

            try
            {
                return type.AssemblyQualifiedName ?? type.ToString();
            }
            catch (Exception)
            {
                return DescribeType(type);
            }
        }

        private static string MethodKey(MethodBase method)
        {
            try
            {
                var text = new StringBuilder();
                text.Append(method is ConstructorInfo ? "0:" : "1:");
                text.Append(method.Name);
                text.Append('(');

                ParameterInfo[] parameters = method.GetParameters();
                for (int i = 0; i < parameters.Length; i++)
                {
                    if (i > 0)
                    {
                        text.Append(',');
                    }

                    text.Append(DescribeType(parameters[i].ParameterType));
                }

                text.Append(')');
                text.Append(method.IsStatic ? ":static" : ":instance");
                if (method.IsGenericMethod)
                {
                    text.Append(":generic");
                    text.Append(method.GetGenericArguments().Length);
                }

                return text.ToString();
            }
            catch (Exception)
            {
                return method.Name;
            }
        }

        private static int SafeMetadataToken(MemberInfo member)
        {
            try
            {
                return member.MetadataToken;
            }
            catch (Exception)
            {
                return 0;
            }
        }

        private static string FieldKey(FieldInfo field)
        {
            Guid moduleId = Guid.Empty;
            Module module = field.Module;
            if (module != null)
            {
                try
                {
                    moduleId = module.ModuleVersionId;
                }
                catch (Exception)
                {
                    // A dynamic field without a module version still dedupes by declaring type,
                    // token and name.
                }
            }

            return moduleId.ToString("N") + "|" + DescribeType(field.DeclaringType)
                + "|" + SafeMetadataToken(field) + "|" + field.Name;
        }

        private static string DescribeType(Type type)
        {
            if (type == null)
            {
                return "<null>";
            }

            try
            {
                return type.FullName ?? type.ToString();
            }
            catch (Exception)
            {
                return "<unprintable type>";
            }
        }

        private static string DescribeField(FieldInfo field)
        {
            if (field == null)
            {
                return "<null field>";
            }

            return DescribeType(field.DeclaringType) + "." + field.Name;
        }

        private static string DescribeException(Exception exception)
        {
            if (exception == null)
            {
                return "unknown error";
            }

            var text = new StringBuilder();
            text.Append(exception.GetType().Name);
            if (!string.IsNullOrEmpty(exception.Message))
            {
                text.Append(": ");
                text.Append(exception.Message);
            }

            if (exception.InnerException != null && !string.IsNullOrEmpty(exception.InnerException.Message))
            {
                text.Append(" (");
                text.Append(exception.InnerException.GetType().Name);
                text.Append(": ");
                text.Append(exception.InnerException.Message);
                text.Append(')');
            }

            return text.ToString();
        }

        private static string FormatOffset(int offset)
        {
            return "0x" + offset.ToString("X4");
        }

        private static bool IsFatal(Exception exception)
        {
            return exception is OutOfMemoryException;
        }
    }

    /// <summary>
    /// The evidence collected by <see cref="IlWriteScanner"/>: direct field stores and every reason
    /// the scan could not cover all candidate method bodies.
    /// </summary>
    /// <remarks>
    /// <para>
    /// A direct write is a <c>stfld</c> or <c>stsfld</c> instruction in a scanned method body. It is
    /// not a classification of the field as configuration, and its absence in a complete scan only
    /// means that these methods do not directly store the field.
    /// </para>
    /// <para>
    /// <see cref="IsComplete"/> is false whenever the method cap was reached or any issue was
    /// recorded, including an indirect address of a field. Consumers must not report "no writes
    /// found" for a field unless <see cref="IsComplete"/> is true.
    /// </para>
    /// </remarks>
    public sealed class IlWriteScanResult
    {
        internal IlWriteScanResult(
            IReadOnlyList<IlWrite> writes,
            IReadOnlyList<IlWriteScanIssue> issues,
            int totalMethods,
            int scannedMethods,
            bool limitReached)
        {
            Writes = writes;
            Issues = issues;
            TotalMethods = totalMethods;
            ScannedMethods = scannedMethods;
            LimitReached = limitReached;
        }

        /// <summary>Direct field stores found in the scanned method bodies.</summary>
        public IReadOnlyList<IlWrite> Writes { get; }

        /// <summary>Every reason the scan could not be exhaustive, in scan order.</summary>
        public IReadOnlyList<IlWriteScanIssue> Issues { get; }

        /// <summary>
        /// Number of non-abstract methods and constructors declared by the supplied types, across all
        /// provided types. Abstract methods are excluded because they cannot contain IL.
        /// </summary>
        public int TotalMethods { get; }

        /// <summary>
        /// Number of method bodies the scan started to examine. A body that failed to decode is still
        /// counted here and is also reported through <see cref="Issues"/>.
        /// </summary>
        public int ScannedMethods { get; }

        /// <summary>
        /// Methods not examined because the cap was reached: <see cref="TotalMethods"/> minus
        /// <see cref="ScannedMethods"/>.
        /// </summary>
        public int SkippedMethods
        {
            get { return TotalMethods - ScannedMethods; }
        }

        /// <summary>Whether <c>maxMethods</c> stopped the scan before every candidate was examined.</summary>
        public bool LimitReached { get; }

        /// <summary>
        /// True only when every candidate method body was examined and every issue was resolved. A
        /// complete scan is still direct-write evidence only, not a runtime or configuration
        /// classification.
        /// </summary>
        public bool IsComplete
        {
            get { return !LimitReached && Issues.Count == 0; }
        }
    }

    /// <summary>A direct <c>stfld</c> or <c>stsfld</c> in one method body.</summary>
    public sealed class IlWrite
    {
        internal IlWrite(FieldInfo field, MethodBase method, int ilOffset)
        {
            Field = field;
            Method = method;
            IlOffset = ilOffset;
        }

        /// <summary>The field whose token was passed to the store instruction.</summary>
        public FieldInfo Field { get; }

        /// <summary>The method or constructor containing the store.</summary>
        public MethodBase Method { get; }

        /// <summary>IL offset of the first direct store of this field in this method.</summary>
        public int IlOffset { get; }
    }

    /// <summary>One reason the direct-write scan is incomplete or a potential indirect write.</summary>
    public sealed class IlWriteScanIssue
    {
        internal IlWriteScanIssue(
            IlWriteScanIssueKind kind,
            MethodBase method,
            Type type,
            FieldInfo field,
            int ilOffset,
            string reason)
        {
            Kind = kind;
            Method = method;
            Type = type;
            Field = field;
            IlOffset = ilOffset;
            Reason = reason ?? string.Empty;
        }

        /// <summary>The category of the issue.</summary>
        public IlWriteScanIssueKind Kind { get; }

        /// <summary>
        /// The affected method, or null when the failure is about the type sequence or a whole type
        /// rather than one method body.
        /// </summary>
        public MethodBase Method { get; }

        /// <summary>The affected type when the issue is type-level; otherwise the method's declaring type, or null.</summary>
        public Type Type { get; }

        /// <summary>The addressed field for an indirect write when its token resolved; otherwise null.</summary>
        public FieldInfo Field { get; }

        /// <summary>IL offset for decode, token and indirect-write issues; -1 otherwise.</summary>
        public int IlOffset { get; }

        /// <summary>A human-readable account of what is unknown and why.</summary>
        public string Reason { get; }

        internal static IlWriteScanIssue ForType(Type type, IlWriteScanIssueKind kind, string reason)
        {
            return new IlWriteScanIssue(kind, null, type, null, -1, reason);
        }

        internal static IlWriteScanIssue ForMethod(
            MethodBase method,
            IlWriteScanIssueKind kind,
            int ilOffset,
            string reason)
        {
            return new IlWriteScanIssue(
                kind,
                method,
                method == null ? null : method.DeclaringType,
                null,
                ilOffset,
                reason);
        }

        internal static IlWriteScanIssue ForField(
            MethodBase method,
            int ilOffset,
            IlWriteScanIssueKind kind,
            FieldInfo field,
            string reason)
        {
            return new IlWriteScanIssue(
                kind,
                method,
                method == null ? null : method.DeclaringType,
                field,
                ilOffset,
                reason);
        }
    }

    /// <summary>Why an <see cref="IlWriteScanIssue"/> was recorded.</summary>
    public enum IlWriteScanIssueKind
    {
        /// <summary>The provided type sequence threw or contained an unusable entry.</summary>
        TypeEnumerationFailed,

        /// <summary>Reflection over a type's declared members threw, so its writes are unknown.</summary>
        TypeInaccessible,

        /// <summary>A non-abstract method had no readable IL body.</summary>
        MissingMethodBody,

        /// <summary>The IL stream could not be decoded to its end; later writes in that method are unknown.</summary>
        DecodeFailed,

        /// <summary>A field token in a direct store could not be resolved.</summary>
        UnresolvedFieldToken,

        /// <summary>An address-of-field instruction may cause a write that this scan cannot follow.</summary>
        IndirectWrite,
    }
}
