using System;
using System.Collections.Generic;
using System.Collections.Immutable;
using System.IO;
using System.Reflection;
using System.Reflection.Metadata;
using System.Reflection.Metadata.Ecma335;
using System.Reflection.PortableExecutable;

namespace Playtest.IlScan
{
    /// <summary>Resolves the field token passed to a store or address instruction.</summary>
    public interface IFieldTokenResolver
    {
        /// <summary>
        /// Returns the field that <paramref name="token"/> names in <paramref name="module"/>, using the
        /// generic context of the method that contains the instruction, or throws with the reason it
        /// cannot be resolved.
        /// </summary>
        FieldInfo ResolveField(Module module, int token, Type[] typeArguments, Type[] methodArguments);
    }

    /// <summary>
    /// Resolves field tokens for assemblies loaded into a <see cref="MetadataLoadContext"/>, which does
    /// not resolve tokens itself, by reading each module's metadata tables.
    /// </summary>
    /// <remarks>
    /// It follows the rules of Mono, the Editor's runtime, because the ids derived from the result must
    /// match the ids discovery derives from live reflection:
    /// <list type="number">
    /// <item>A field definition token resolves to that field on its declaring type definition.</item>
    /// <item>A member reference resolves its parent type with the method's generic context, then takes
    /// the first field with the referenced name and field signature on that type or, failing that, on
    /// its base types. The signature is compared with the field's declared type, matching type
    /// parameters by position.</item>
    /// <item>The declaring type is normalized as Mono normalizes reflection types: a generic type
    /// instantiated over its own type parameters is the generic type definition. A store inside a
    /// generic type therefore names the same field as the type's declaration.</item>
    /// <item>A type reference resolves by namespace and name in the referenced assembly, following type
    /// forwarders. No parsed type name string is matched.</item>
    /// </list>
    /// Every failure throws, so the scanner records an unresolved token rather than a missing write.
    /// </remarks>
    public sealed class MetadataFieldResolver : IFieldTokenResolver, IDisposable
    {
        private const BindingFlags DeclaredFields = BindingFlags.DeclaredOnly | BindingFlags.Instance
            | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;
        private const int MaximumForwardingDepth = 32;

        private readonly MetadataLoadContext _context;
        private readonly TypeProvider _types;
        private readonly Dictionary<Module, ModuleMetadata> _modules = new Dictionary<Module, ModuleMetadata>();
        private readonly Dictionary<MetadataReader, ModuleMetadata> _readers = new Dictionary<MetadataReader, ModuleMetadata>();
        private readonly Dictionary<PrimitiveTypeCode, Type> _primitives = new Dictionary<PrimitiveTypeCode, Type>();
        private readonly Dictionary<(Module, int), FieldInfo> _contextFree = new Dictionary<(Module, int), FieldInfo>();
        private bool _disposed;

        public MetadataFieldResolver(MetadataLoadContext context)
        {
            _context = context ?? throw new ArgumentNullException(nameof(context));
            _types = new TypeProvider(this);
        }

        public FieldInfo ResolveField(Module module, int token, Type[] typeArguments, Type[] methodArguments)
        {
            if (module == null) throw new ArgumentNullException(nameof(module));
            if (_disposed) throw new ObjectDisposedException(nameof(MetadataFieldResolver));

            bool contextFree = typeArguments == null && methodArguments == null;
            if (contextFree && _contextFree.TryGetValue((module, token), out FieldInfo cached)) return cached;

            ModuleMetadata metadata = Metadata(module);
            int table = (int)((uint)token >> 24);
            int row = token & 0x00FFFFFF;
            FieldInfo field;
            if (table == (int)TableIndex.Field)
            {
                if (row < 1 || row > metadata.Reader.GetTableRowCount(TableIndex.Field))
                    throw new BadImageFormatException("field token " + Hex(token) + " is outside the field table");
                field = FieldDefinition(metadata, MetadataTokens.FieldDefinitionHandle(row));
            }
            else if (table == (int)TableIndex.MemberRef)
            {
                if (row < 1 || row > metadata.Reader.GetTableRowCount(TableIndex.MemberRef))
                    throw new BadImageFormatException("member reference token " + Hex(token) + " is outside the member reference table");
                field = MemberReferenceField(
                    metadata, MetadataTokens.MemberReferenceHandle(row), new GenericContext(typeArguments, methodArguments));
            }
            else
            {
                throw new BadImageFormatException("token " + Hex(token) + " is neither a field definition nor a member reference");
            }

            if (contextFree) _contextFree[(module, token)] = field;
            return field;
        }

        public void Dispose()
        {
            if (_disposed) return;
            _disposed = true;
            foreach (ModuleMetadata metadata in _modules.Values) metadata.Dispose();
            _modules.Clear();
            _readers.Clear();
        }

        /// <summary>
        /// Mono's normalization of a reflection type: an instantiation of a generic type over its own
        /// type parameters is that generic type definition, applied to type arguments recursively.
        /// </summary>
        public static Type Normalize(Type type)
        {
            if (type == null) throw new ArgumentNullException(nameof(type));
            if (!type.IsConstructedGenericType || !type.ContainsGenericParameters) return type;

            Type definition = type.GetGenericTypeDefinition();
            Type[] arguments = type.GetGenericArguments();
            bool identity = true;
            bool rebind = false;
            var normalized = new Type[arguments.Length];
            for (int i = 0; i < arguments.Length; i++)
            {
                Type argument = arguments[i];
                if (!argument.IsGenericParameter || argument.IsGenericMethodParameter
                    || argument.GenericParameterPosition != i || argument.DeclaringType != definition)
                {
                    identity = false;
                }

                normalized[i] = Normalize(argument);
                if (!ReferenceEquals(normalized[i], argument)) rebind = true;
            }

            if (identity) return definition;
            return rebind ? definition.MakeGenericType(normalized) : type;
        }

        private FieldInfo FieldDefinition(ModuleMetadata metadata, FieldDefinitionHandle handle)
        {
            FieldDefinition definition = metadata.Reader.GetFieldDefinition(handle);
            Type owner = metadata.Type(definition.GetDeclaringType());
            return FieldOn(owner, MetadataTokens.GetToken(handle));
        }

        private FieldInfo MemberReferenceField(ModuleMetadata metadata, MemberReferenceHandle handle, GenericContext context)
        {
            MetadataReader reader = metadata.Reader;
            MemberReference reference = reader.GetMemberReference(handle);
            if (reference.GetKind() != MemberReferenceKind.Field)
                throw new BadImageFormatException("member reference " + Hex(MetadataTokens.GetToken(handle)) + " is not a field reference");

            string name = reader.GetString(reference.Name);
            Type parent = Parent(metadata, reference.Parent, context);
            for (Type level = parent; level != null; level = level.BaseType)
            {
                Type definition = level.IsConstructedGenericType ? level.GetGenericTypeDefinition() : level;
                FieldInfo[] candidates = definition.GetFields(DeclaredFields);
                Array.Sort(candidates, (left, right) => left.MetadataToken.CompareTo(right.MetadataToken));
                foreach (FieldInfo candidate in candidates)
                {
                    if (!string.Equals(candidate.Name, name, StringComparison.Ordinal)) continue;
                    if (!SignatureMatches(reader, reference, definition, candidate)) continue;

                    Type declaring = Normalize(level);
                    return ReferenceEquals(declaring, definition) ? candidate : FieldOn(declaring, candidate.MetadataToken);
                }
            }

            throw new MissingFieldException("no field named '" + name + "' with the referenced signature is declared by '"
                + Describe(parent) + "' or its base types");
        }

        private bool SignatureMatches(MetadataReader reader, MemberReference reference, Type definition, FieldInfo candidate)
        {
            // A field reference states the declared field type against the parent's own type parameters.
            var context = new GenericContext(definition.IsGenericTypeDefinition ? definition.GetGenericArguments() : null, null);
            Type referenced = reference.DecodeFieldSignature(_types, context);
            return referenced == candidate.FieldType;
        }

        private Type Parent(ModuleMetadata metadata, EntityHandle parent, GenericContext context)
        {
            switch (parent.Kind)
            {
                case HandleKind.TypeDefinition:
                    return metadata.Type((TypeDefinitionHandle)parent);
                case HandleKind.TypeReference:
                    return TypeReference(metadata, (TypeReferenceHandle)parent);
                case HandleKind.TypeSpecification:
                    return metadata.Reader.GetTypeSpecification((TypeSpecificationHandle)parent).DecodeSignature(_types, context);
                default:
                    throw new NotSupportedException("a field reference whose parent is a " + parent.Kind + " is not supported");
            }
        }

        private Type TypeReference(ModuleMetadata metadata, TypeReferenceHandle handle)
        {
            if (metadata.References.TryGetValue(handle, out Type cached)) return cached;

            MetadataReader reader = metadata.Reader;
            TypeReference reference = reader.GetTypeReference(handle);
            string ns = reader.GetString(reference.Namespace);
            string name = reader.GetString(reference.Name);
            EntityHandle scope = reference.ResolutionScope;
            Type type;
            switch (scope.Kind)
            {
                case HandleKind.TypeReference:
                    type = Nested(TypeReference(metadata, (TypeReferenceHandle)scope), ns, name);
                    break;
                case HandleKind.AssemblyReference:
                    AssemblyName assembly = reader.GetAssemblyReference((AssemblyReferenceHandle)scope).GetAssemblyName();
                    type = InAssembly(_context.LoadFromAssemblyName(assembly), ns, name, 0);
                    break;
                case HandleKind.ModuleDefinition:
                    type = metadata.TopLevel(ns, name)
                        ?? throw new TypeLoadException("type '" + Join(ns, name) + "' is not defined in its own module");
                    break;
                default:
                    throw new NotSupportedException("a type reference scoped to a " + scope.Kind + " is not supported");
            }

            metadata.References[handle] = type;
            return type;
        }

        private Type InAssembly(Assembly assembly, string ns, string name, int depth)
        {
            ModuleMetadata metadata = Metadata(assembly.ManifestModule);
            Type type = metadata.TopLevel(ns, name);
            if (type != null) return type;

            AssemblyName forwardedTo = metadata.Forwarder(ns, name);
            if (forwardedTo == null)
                throw new TypeLoadException("type '" + Join(ns, name) + "' is neither defined nor forwarded by assembly '"
                    + assembly.GetName().Name + "'");
            if (depth >= MaximumForwardingDepth)
                throw new TypeLoadException("type '" + Join(ns, name) + "' is forwarded more than " + MaximumForwardingDepth + " times");
            return InAssembly(_context.LoadFromAssemblyName(forwardedTo), ns, name, depth + 1);
        }

        private Type Nested(Type outer, string ns, string name)
        {
            ModuleMetadata metadata = Metadata(outer.Module);
            MetadataReader reader = metadata.Reader;
            TypeDefinition definition = reader.GetTypeDefinition(MetadataTokens.TypeDefinitionHandle(outer.MetadataToken & 0x00FFFFFF));
            foreach (TypeDefinitionHandle nested in definition.GetNestedTypes())
            {
                TypeDefinition candidate = reader.GetTypeDefinition(nested);
                if (reader.StringComparer.Equals(candidate.Name, name) && reader.StringComparer.Equals(candidate.Namespace, ns))
                    return metadata.Type(nested);
            }

            throw new TypeLoadException("type '" + Describe(outer) + "' has no nested type '" + Join(ns, name) + "'");
        }

        private Type Primitive(PrimitiveTypeCode code)
        {
            if (_primitives.TryGetValue(code, out Type cached)) return cached;
            Assembly core = _context.CoreAssembly
                ?? throw new FileNotFoundException("no core assembly was found among the references");
            Type type = InAssembly(core, "System", PrimitiveName(code), 0);
            _primitives[code] = type;
            return type;
        }

        private ModuleMetadata Metadata(Module module)
        {
            if (_modules.TryGetValue(module, out ModuleMetadata metadata)) return metadata;

            string path = module.FullyQualifiedName;
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
                throw new FileNotFoundException("module '" + module.Name + "' has no metadata file to read");
            var pe = new PEReader(File.OpenRead(path));
            try
            {
                MetadataReader reader = pe.GetMetadataReader();
                if (reader.GetGuid(reader.GetModuleDefinition().Mvid) != module.ModuleVersionId)
                    throw new InvalidOperationException("module '" + module.Name + "' changed on disk after it was loaded");
                metadata = new ModuleMetadata(module, pe, reader);
            }
            catch
            {
                pe.Dispose();
                throw;
            }

            _modules.Add(module, metadata);
            _readers.Add(metadata.Reader, metadata);
            return metadata;
        }

        private ModuleMetadata ForReader(MetadataReader reader)
        {
            if (_readers.TryGetValue(reader, out ModuleMetadata metadata)) return metadata;
            throw new InvalidOperationException("a signature was decoded with an unknown metadata reader");
        }

        private static FieldInfo FieldOn(Type type, int token)
        {
            foreach (FieldInfo field in type.GetFields(DeclaredFields))
            {
                if (field.MetadataToken == token) return field;
            }

            throw new MissingFieldException("type '" + Describe(type) + "' declares no field with token " + Hex(token));
        }

        private static string PrimitiveName(PrimitiveTypeCode code)
        {
            switch (code)
            {
                case PrimitiveTypeCode.Boolean: return "Boolean";
                case PrimitiveTypeCode.Byte: return "Byte";
                case PrimitiveTypeCode.SByte: return "SByte";
                case PrimitiveTypeCode.Char: return "Char";
                case PrimitiveTypeCode.Int16: return "Int16";
                case PrimitiveTypeCode.UInt16: return "UInt16";
                case PrimitiveTypeCode.Int32: return "Int32";
                case PrimitiveTypeCode.UInt32: return "UInt32";
                case PrimitiveTypeCode.Int64: return "Int64";
                case PrimitiveTypeCode.UInt64: return "UInt64";
                case PrimitiveTypeCode.Single: return "Single";
                case PrimitiveTypeCode.Double: return "Double";
                case PrimitiveTypeCode.IntPtr: return "IntPtr";
                case PrimitiveTypeCode.UIntPtr: return "UIntPtr";
                case PrimitiveTypeCode.Object: return "Object";
                case PrimitiveTypeCode.String: return "String";
                case PrimitiveTypeCode.TypedReference: return "TypedReference";
                case PrimitiveTypeCode.Void: return "Void";
                default: throw new BadImageFormatException("unknown primitive type code " + (int)code);
            }
        }

        private static string Join(string ns, string name) => string.IsNullOrEmpty(ns) ? name : ns + "." + name;

        private static string Hex(int token) => "0x" + token.ToString("X8");

        private static string Describe(Type type)
        {
            try
            {
                return type.FullName ?? type.ToString();
            }
            catch (Exception)
            {
                return "<unprintable type>";
            }
        }

        private sealed class ModuleMetadata : IDisposable
        {
            public readonly Module Module;
            public readonly MetadataReader Reader;
            public readonly Dictionary<TypeReferenceHandle, Type> References = new Dictionary<TypeReferenceHandle, Type>();
            private readonly PEReader _pe;
            private Dictionary<int, Type> _definitions;
            private Dictionary<(string, string), TypeDefinitionHandle> _topLevel;
            private Dictionary<(string, string), AssemblyName> _forwarders;

            public ModuleMetadata(Module module, PEReader pe, MetadataReader reader)
            {
                Module = module;
                _pe = pe;
                Reader = reader;
            }

            public Type Type(TypeDefinitionHandle handle)
            {
                if (_definitions == null)
                {
                    _definitions = new Dictionary<int, Type>();
                    foreach (Type type in Module.GetTypes()) _definitions[type.MetadataToken] = type;
                }

                int token = MetadataTokens.GetToken(handle);
                if (_definitions.TryGetValue(token, out Type found)) return found;
                throw new TypeLoadException("module '" + Module.Name + "' exposes no type for definition " + Hex(token));
            }

            public Type TopLevel(string ns, string name)
            {
                if (_topLevel == null)
                {
                    _topLevel = new Dictionary<(string, string), TypeDefinitionHandle>();
                    foreach (TypeDefinitionHandle handle in Reader.TypeDefinitions)
                    {
                        TypeDefinition definition = Reader.GetTypeDefinition(handle);
                        if (definition.IsNested) continue;
                        var key = (Reader.GetString(definition.Namespace), Reader.GetString(definition.Name));
                        if (!_topLevel.ContainsKey(key)) _topLevel.Add(key, handle);
                    }
                }

                return _topLevel.TryGetValue((ns, name), out TypeDefinitionHandle found) ? Type(found) : null;
            }

            public AssemblyName Forwarder(string ns, string name)
            {
                if (_forwarders == null)
                {
                    _forwarders = new Dictionary<(string, string), AssemblyName>();
                    foreach (ExportedTypeHandle handle in Reader.ExportedTypes)
                    {
                        ExportedType exported = Reader.GetExportedType(handle);
                        if (!exported.IsForwarder || exported.Implementation.Kind != HandleKind.AssemblyReference) continue;
                        var key = (Reader.GetString(exported.Namespace), Reader.GetString(exported.Name));
                        if (!_forwarders.ContainsKey(key))
                            _forwarders.Add(key, Reader.GetAssemblyReference((AssemblyReferenceHandle)exported.Implementation).GetAssemblyName());
                    }
                }

                return _forwarders.TryGetValue((ns, name), out AssemblyName found) ? found : null;
            }

            public void Dispose() => _pe.Dispose();
        }

        private sealed class GenericContext
        {
            private readonly Type[] _typeArguments;
            private readonly Type[] _methodArguments;

            public GenericContext(Type[] typeArguments, Type[] methodArguments)
            {
                _typeArguments = typeArguments;
                _methodArguments = methodArguments;
            }

            public Type TypeParameter(int index)
            {
                if (_typeArguments == null || index < 0 || index >= _typeArguments.Length)
                    throw new BadImageFormatException("type parameter !" + index + " is outside the method's generic context");
                return _typeArguments[index];
            }

            public Type MethodParameter(int index)
            {
                if (_methodArguments == null || index < 0 || index >= _methodArguments.Length)
                    throw new BadImageFormatException("method type parameter !!" + index + " is outside the method's generic context");
                return _methodArguments[index];
            }
        }

        private sealed class TypeProvider : ISignatureTypeProvider<Type, GenericContext>
        {
            private readonly MetadataFieldResolver _owner;

            public TypeProvider(MetadataFieldResolver owner) { _owner = owner; }

            public Type GetPrimitiveType(PrimitiveTypeCode typeCode) => _owner.Primitive(typeCode);

            public Type GetTypeFromDefinition(MetadataReader reader, TypeDefinitionHandle handle, byte rawTypeKind)
                => _owner.ForReader(reader).Type(handle);

            public Type GetTypeFromReference(MetadataReader reader, TypeReferenceHandle handle, byte rawTypeKind)
                => _owner.TypeReference(_owner.ForReader(reader), handle);

            public Type GetTypeFromSpecification(
                MetadataReader reader, GenericContext genericContext, TypeSpecificationHandle handle, byte rawTypeKind)
                => reader.GetTypeSpecification(handle).DecodeSignature(this, genericContext);

            public Type GetSZArrayType(Type elementType) => elementType.MakeArrayType();

            public Type GetArrayType(Type elementType, ArrayShape shape) => elementType.MakeArrayType(shape.Rank);

            public Type GetByReferenceType(Type elementType) => elementType.MakeByRefType();

            public Type GetPointerType(Type elementType) => elementType.MakePointerType();

            public Type GetGenericInstantiation(Type genericType, ImmutableArray<Type> typeArguments)
            {
                var arguments = new Type[typeArguments.Length];
                typeArguments.CopyTo(arguments);
                return genericType.MakeGenericType(arguments);
            }

            public Type GetGenericTypeParameter(GenericContext genericContext, int index) => genericContext.TypeParameter(index);

            public Type GetGenericMethodParameter(GenericContext genericContext, int index) => genericContext.MethodParameter(index);

            public Type GetFunctionPointerType(MethodSignature<Type> signature)
                => throw new NotSupportedException("function pointer types are not supported");

            // Modifiers do not change which field or type a token names.
            public Type GetModifiedType(Type modifier, Type unmodifiedType, bool isRequired) => unmodifiedType;

            public Type GetPinnedType(Type elementType) => elementType;
        }
    }
}
