using System;
using System.Collections.Generic;
using System.Text;
using NUnit.Framework;
using static Playtest.IlScan.Fixtures.ScanTargets;

namespace Playtest.IlScan.Tests
{
    /// <summary>
    /// The scanner's tests from the bridge package, run on the targets loaded for inspection only, as the
    /// tool loads a project's assemblies.
    /// </summary>
    [TestFixture]
    public sealed class IlWriteScannerTests
    {
        private InspectionScope _scope;

        [OneTimeSetUp]
        public void Load() => _scope = new InspectionScope();

        [OneTimeTearDown]
        public void Unload() => _scope.Dispose();

        [Test]
        public void Scan_ReportsDirectInstanceAndStaticWrites()
        {
            IlWriteScanResult result = Scan(typeof(DirectWriteTarget));

            Assert.That(result.IsComplete, Is.True, Describe(result));
            Assert.That(result.Issues, Is.Empty, Describe(result));
            Assert.That(result.LimitReached, Is.False);
            Assert.That(result.TotalMethods, Is.EqualTo(3), Describe(result));
            Assert.That(result.ScannedMethods, Is.EqualTo(3), Describe(result));
            Assert.That(result.SkippedMethods, Is.EqualTo(0), Describe(result));

            AssertHasWrite(result, typeof(DirectWriteTarget), "InstanceValue", "SetInstance");
            AssertHasWrite(result, typeof(DirectWriteTarget), "StaticValue", "SetStatic");
            AssertHasWrite(result, typeof(DirectWriteTarget), "ConstructorValue", ".ctor");
            Assert.That(CountWrites(result, typeof(DirectWriteTarget), "NeverWritten"), Is.EqualTo(0), Describe(result));
        }

        [Test]
        public void Scan_CapturesStaticConstructorWrites()
        {
            IlWriteScanResult result = Scan(typeof(StaticConstructorTarget));

            Assert.That(result.IsComplete, Is.True, Describe(result));
            Assert.That(result.Issues, Is.Empty, Describe(result));
            AssertHasWrite(result, typeof(StaticConstructorTarget), "Initialized", ".cctor");
        }

        [Test]
        public void Scan_CapturesWritesToFieldsDeclaredOnOtherTypes()
        {
            IlWriteScanResult result = Scan(typeof(ExternalFieldWriter));

            Assert.That(result.IsComplete, Is.True, Describe(result));
            AssertHasWrite(result, typeof(ExternalFieldHolder), "Value", "Write");
        }

        [Test]
        public void Scan_NoWriteMethod_LeavesItsFieldUnwrittenWhileTheScanIsComplete()
        {
            IlWriteScanResult result = Scan(typeof(NoWriteTarget));

            Assert.That(result.IsComplete, Is.True, Describe(result));
            Assert.That(result.Issues, Is.Empty, Describe(result));
            Assert.That(result.Writes.Count, Is.EqualTo(1), Describe(result));
            AssertHasWrite(result, typeof(NoWriteTarget), "Written", "WriteIt");
            Assert.That(CountWrites(result, typeof(NoWriteTarget), "NeverWritten"), Is.EqualTo(0), Describe(result));
        }

        [Test]
        public void Scan_DecodesOperandLengthsSwitchAndTwoByteOpcodes()
        {
            IlWriteScanResult result = Scan(typeof(OperandTarget));

            Assert.That(result.IsComplete, Is.True, Describe(result));
            Assert.That(result.Issues, Is.Empty, Describe(result));

            AssertHasWrite(result, typeof(OperandTarget), "Selected", "Decode");
            AssertHasWrite(result, typeof(OperandTarget), "LongValue", "Decode");
            AssertHasWrite(result, typeof(OperandTarget), "DoubleValue", "Decode");
            AssertHasWrite(result, typeof(OperandTarget), "FloatValue", "Decode");
            AssertHasWrite(result, typeof(OperandTarget), "TextValue", "Decode");
        }

        [Test]
        public void Scan_ReportsAddressOfFieldAsIndirectWrite()
        {
            IlWriteScanResult result = Scan(typeof(IndirectWriteTarget));

            Assert.That(result.IsComplete, Is.False, "an address of a field leaves the direct write set unresolved");
            Assert.That(result.Issues.Count, Is.EqualTo(2), Describe(result));
            Assert.That(CountWrites(result, typeof(IndirectWriteTarget), "InstanceValue"), Is.EqualTo(0), Describe(result));
            Assert.That(CountWrites(result, typeof(IndirectWriteTarget), "StaticValue"), Is.EqualTo(0), Describe(result));

            Assert.That(
                HasIssue(result, IlWriteScanIssueKind.IndirectWrite, "GetInstanceRef", "InstanceValue"),
                Is.True,
                Describe(result));
            Assert.That(
                HasIssue(result, IlWriteScanIssueKind.IndirectWrite, "GetSharedRef", "StaticValue"),
                Is.True,
                Describe(result));
        }

        [Test]
        public void Scan_RespectsMaxMethodsAndReportsSkippedMethods()
        {
            IlWriteScanResult capped = IlWriteScanner.Scan(new[] { Inspected(typeof(CappedTarget)) }, 2, _scope.Resolver);

            Assert.That(capped.LimitReached, Is.True, Describe(capped));
            Assert.That(capped.IsComplete, Is.False, Describe(capped));
            Assert.That(capped.ScannedMethods, Is.EqualTo(2), Describe(capped));
            Assert.That(capped.TotalMethods, Is.EqualTo(5), Describe(capped));
            Assert.That(capped.SkippedMethods, Is.EqualTo(3), Describe(capped));
            Assert.That(capped.Writes.Count, Is.LessThanOrEqualTo(2), Describe(capped));

            IlWriteScanResult whole = IlWriteScanner.Scan(new[] { Inspected(typeof(CappedTarget)) }, 64, _scope.Resolver);

            Assert.That(whole.IsComplete, Is.True, Describe(whole));
            Assert.That(whole.LimitReached, Is.False);
            Assert.That(whole.TotalMethods, Is.EqualTo(5), Describe(whole));
            Assert.That(whole.ScannedMethods, Is.EqualTo(5), Describe(whole));
            Assert.That(whole.SkippedMethods, Is.EqualTo(0), Describe(whole));
            AssertHasWrite(whole, typeof(CappedTarget), "A", "WriteA");
            AssertHasWrite(whole, typeof(CappedTarget), "B", "WriteB");
            AssertHasWrite(whole, typeof(CappedTarget), "C", "WriteC");
            AssertHasWrite(whole, typeof(CappedTarget), "D", "WriteD");
        }

        [Test]
        public void Scan_RejectsNonPositiveCap()
        {
            Type type = Inspected(typeof(NoWriteTarget));
            Assert.Throws<ArgumentOutOfRangeException>(() => IlWriteScanner.Scan(new[] { type }, 0, _scope.Resolver));
            Assert.Throws<ArgumentOutOfRangeException>(() => IlWriteScanner.Scan(new[] { type }, -1, _scope.Resolver));
        }

        [Test]
        public void Scan_RejectsNullTypeSequenceAndResolver()
        {
            Assert.Throws<ArgumentNullException>(() => IlWriteScanner.Scan(null, 1, _scope.Resolver));
            Assert.Throws<ArgumentNullException>(() => IlWriteScanner.Scan(new[] { Inspected(typeof(NoWriteTarget)) }, 1, null));
        }

        [Test]
        public void Scan_ReportsMissingBodyForNonAbstractMethod()
        {
            IlWriteScanResult result = Scan(typeof(MissingBodyTarget));

            Assert.That(result.IsComplete, Is.False, Describe(result));
            Assert.That(result.SkippedMethods, Is.EqualTo(0), Describe(result));
            Assert.That(result.Writes, Is.Empty, Describe(result));
            Assert.That(
                HasIssue(result, IlWriteScanIssueKind.MissingMethodBody, "NativeCall", null),
                Is.True,
                Describe(result));
        }

        [Test]
        public void Scan_AbstractMethodsAreNotReportedAsMissingBodies()
        {
            IlWriteScanResult result = Scan(typeof(AbstractTarget));

            Assert.That(result.IsComplete, Is.True, Describe(result));
            Assert.That(result.Issues, Is.Empty, Describe(result));
            Assert.That(result.TotalMethods, Is.EqualTo(2), Describe(result));
            AssertHasWrite(result, typeof(AbstractTarget), "ConcreteValue", "WriteConcrete");
        }

        [Test]
        public void Scan_ReportsTypeEnumerationFailureWithNullMethod()
        {
            IlWriteScanResult result = IlWriteScanner.Scan(ThrowingTypes(), 64, _scope.Resolver);

            Assert.That(result.IsComplete, Is.False, Describe(result));
            Assert.That(result.Issues.Count, Is.EqualTo(1), Describe(result));
            Assert.That(result.Issues[0].Kind, Is.EqualTo(IlWriteScanIssueKind.TypeEnumerationFailed));
            Assert.That(result.Issues[0].Method, Is.Null);
            Assert.That(result.Issues[0].Reason, Does.Contain("enumeration failed"));
            Assert.That(result.Writes.Count, Is.EqualTo(1), "partial evidence before the enumeration failure must be kept");
        }

        [Test]
        public void Scan_ReportsNullTypeEntryWithoutCrashing()
        {
            IlWriteScanResult result = IlWriteScanner.Scan(new Type[] { null, Inspected(typeof(NoWriteTarget)) }, 64, _scope.Resolver);

            Assert.That(result.IsComplete, Is.False, Describe(result));
            Assert.That(result.Issues.Count, Is.EqualTo(1), Describe(result));
            Assert.That(result.Issues[0].Kind, Is.EqualTo(IlWriteScanIssueKind.TypeEnumerationFailed));
            Assert.That(result.Issues[0].Method, Is.Null);
            Assert.That(result.Issues[0].Reason, Does.Contain("null entry"));
            Assert.That(result.Writes.Count, Is.EqualTo(1), Describe(result));
        }

        [Test]
        public void Scan_DoesNotTraverseBaseTypes()
        {
            IlWriteScanResult result = Scan(typeof(DerivedWriteTarget));

            Assert.That(result.IsComplete, Is.True, Describe(result));
            AssertHasWrite(result, typeof(DerivedWriteTarget), "DerivedValue", "WriteDerived");
            Assert.That(CountWrites(result, typeof(BaseWriteTarget), "BaseValue"), Is.EqualTo(0), Describe(result));

            for (int i = 0; i < result.Writes.Count; i++)
            {
                Assert.That(
                    result.Writes[i].Method.DeclaringType,
                    Is.Not.EqualTo(Inspected(typeof(BaseWriteTarget))),
                    "a type scan must not include methods from its base type");
            }
        }

        [Test]
        public void Scan_ResolvesFieldTokensWithGenericTypeAndMethodContext()
        {
            IlWriteScanResult closed = Scan(typeof(GenericHolder<int>));

            Assert.That(closed.IsComplete, Is.True, Describe(closed));
            IlWrite closedWrite = FindWrite(closed, Inspected(typeof(GenericHolder<int>)), "Value", "Set");
            Assert.That(closedWrite, Is.Not.Null, Describe(closed));
            Assert.That(
                closedWrite.Field.FieldType,
                Is.EqualTo(Inspected(typeof(int))),
                "the field token must be resolved with the constructed type's generic arguments");

            IlWriteScanResult open = Scan(typeof(GenericHolder<>));

            Assert.That(open.IsComplete, Is.True, Describe(open));
            Assert.That(
                FindWrite(open, Inspected(typeof(GenericHolder<>)), "Value", "Set"),
                Is.Not.Null,
                "a store inside a generic type must name the field its definition declares" + Environment.NewLine + Describe(open));

            IlWriteScanResult genericMethod = Scan(typeof(GenericMethodTarget));

            Assert.That(genericMethod.IsComplete, Is.True, Describe(genericMethod));
            IlWrite methodWrite = FindWrite(genericMethod, "Value", "SetBox");
            Assert.That(
                methodWrite,
                Is.Not.Null,
                "the field token must be resolved with the method's generic arguments" + Environment.NewLine + Describe(genericMethod));
        }

        [Test]
        public void Scan_ProducesDeterministicWriteOrder()
        {
            IlWriteScanResult first = IlWriteScanner.Scan(
                new[] { Inspected(typeof(OperandTarget)), Inspected(typeof(DirectWriteTarget)) },
                64,
                _scope.Resolver);
            IlWriteScanResult second = IlWriteScanner.Scan(
                new[] { Inspected(typeof(DirectWriteTarget)), Inspected(typeof(OperandTarget)) },
                64,
                _scope.Resolver);

            Assert.That(first.IsComplete, Is.True, Describe(first));
            Assert.That(second.IsComplete, Is.True, Describe(second));
            Assert.That(first.Writes.Count, Is.EqualTo(second.Writes.Count));

            for (int i = 0; i < first.Writes.Count; i++)
            {
                Assert.That(first.Writes[i].Method, Is.EqualTo(second.Writes[i].Method));
                Assert.That(first.Writes[i].Field, Is.EqualTo(second.Writes[i].Field));
                Assert.That(first.Writes[i].IlOffset, Is.EqualTo(second.Writes[i].IlOffset));
            }
        }

        private Type Inspected(Type live) => _scope.Type(live);

        private IlWriteScanResult Scan(Type live)
        {
            return IlWriteScanner.Scan(new[] { Inspected(live) }, 64, _scope.Resolver);
        }

        private static IlWrite FindWrite(
            IlWriteScanResult result,
            Type declaringType,
            string fieldName,
            string methodName)
        {
            for (int i = 0; i < result.Writes.Count; i++)
            {
                IlWrite write = result.Writes[i];
                if (write.Field.Name == fieldName
                    && write.Field.DeclaringType == declaringType
                    && write.Method.Name == methodName)
                {
                    return write;
                }
            }

            return null;
        }

        private static IlWrite FindWrite(IlWriteScanResult result, string fieldName, string methodName)
        {
            for (int i = 0; i < result.Writes.Count; i++)
            {
                IlWrite write = result.Writes[i];
                if (write.Field.Name == fieldName && write.Method.Name == methodName)
                {
                    return write;
                }
            }

            return null;
        }

        private int CountWrites(IlWriteScanResult result, Type declaringType, string fieldName)
        {
            Type inspected = Inspected(declaringType);
            int count = 0;
            for (int i = 0; i < result.Writes.Count; i++)
            {
                IlWrite write = result.Writes[i];
                if (write.Field.Name == fieldName && write.Field.DeclaringType == inspected)
                {
                    count++;
                }
            }

            return count;
        }

        private static bool HasIssue(
            IlWriteScanResult result,
            IlWriteScanIssueKind kind,
            string methodName,
            string fieldName)
        {
            for (int i = 0; i < result.Issues.Count; i++)
            {
                IlWriteScanIssue issue = result.Issues[i];
                if (issue.Kind != kind)
                {
                    continue;
                }

                if (methodName != null && (issue.Method == null || issue.Method.Name != methodName))
                {
                    continue;
                }

                if (fieldName != null && (issue.Field == null || issue.Field.Name != fieldName))
                {
                    continue;
                }

                return true;
            }

            return false;
        }

        private void AssertHasWrite(
            IlWriteScanResult result,
            Type declaringType,
            string fieldName,
            string methodName)
        {
            IlWrite write = FindWrite(result, Inspected(declaringType), fieldName, methodName);
            Assert.That(
                write,
                Is.Not.Null,
                "expected a direct write to " + declaringType.Name + "." + fieldName + " in " + methodName
                + Environment.NewLine + Describe(result));
        }

        private static string Describe(IlWriteScanResult result)
        {
            var text = new StringBuilder();
            text.Append("complete=").Append(result.IsComplete)
                .Append(", total=").Append(result.TotalMethods)
                .Append(", scanned=").Append(result.ScannedMethods)
                .Append(", skipped=").Append(result.SkippedMethods)
                .Append(", limit=").Append(result.LimitReached);

            for (int i = 0; i < result.Issues.Count; i++)
            {
                IlWriteScanIssue issue = result.Issues[i];
                text.AppendLine();
                text.Append("  ").Append(issue.Kind).Append(": ").Append(issue.Reason);
            }

            return text.ToString();
        }

        private IEnumerable<Type> ThrowingTypes()
        {
            yield return Inspected(typeof(NoWriteTarget));
            throw new InvalidOperationException("type enumeration failed on purpose");
        }
    }
}
