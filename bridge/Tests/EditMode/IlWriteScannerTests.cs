using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using NUnit.Framework;
using Playtest.Bridge.Editor.Preflight;

namespace Playtest.Bridge.Tests
{
    [TestFixture]
    public sealed class IlWriteScannerTests
    {
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
            IlWriteScanResult capped = IlWriteScanner.Scan(new[] { typeof(CappedTarget) }, 2);

            Assert.That(capped.LimitReached, Is.True, Describe(capped));
            Assert.That(capped.IsComplete, Is.False, Describe(capped));
            Assert.That(capped.ScannedMethods, Is.EqualTo(2), Describe(capped));
            Assert.That(capped.TotalMethods, Is.EqualTo(5), Describe(capped));
            Assert.That(capped.SkippedMethods, Is.EqualTo(3), Describe(capped));
            Assert.That(capped.Writes.Count, Is.LessThanOrEqualTo(2), Describe(capped));

            IlWriteScanResult whole = IlWriteScanner.Scan(new[] { typeof(CappedTarget) }, 64);

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
            Assert.Throws<ArgumentOutOfRangeException>(() => IlWriteScanner.Scan(new[] { typeof(NoWriteTarget) }, 0));
            Assert.Throws<ArgumentOutOfRangeException>(() => IlWriteScanner.Scan(new[] { typeof(NoWriteTarget) }, -1));
        }

        [Test]
        public void Scan_RejectsNullTypeSequence()
        {
            Assert.Throws<ArgumentNullException>(() => IlWriteScanner.Scan(null, 1));
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
            IlWriteScanResult result = IlWriteScanner.Scan(ThrowingTypes(), 64);

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
            IlWriteScanResult result = IlWriteScanner.Scan(new Type[] { null, typeof(NoWriteTarget) }, 64);

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
                    Is.Not.EqualTo(typeof(BaseWriteTarget)),
                    "a type scan must not include methods from its base type");
            }
        }

        [Test]
        public void Scan_ResolvesFieldTokensWithGenericTypeAndMethodContext()
        {
            IlWriteScanResult closed = Scan(typeof(GenericHolder<int>));

            Assert.That(closed.IsComplete, Is.True, Describe(closed));
            IlWrite closedWrite = FindWrite(closed, typeof(GenericHolder<int>), "Value", "Set");
            Assert.That(closedWrite, Is.Not.Null, Describe(closed));
            Assert.That(
                closedWrite.Field.FieldType,
                Is.EqualTo(typeof(int)),
                "the field token must be resolved with the constructed type's generic arguments");

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
                new[] { typeof(OperandTarget), typeof(DirectWriteTarget) },
                64);
            IlWriteScanResult second = IlWriteScanner.Scan(
                new[] { typeof(DirectWriteTarget), typeof(OperandTarget) },
                64);

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

        private static IlWriteScanResult Scan(Type type)
        {
            return IlWriteScanner.Scan(new[] { type }, 64);
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

        private static int CountWrites(IlWriteScanResult result, Type declaringType, string fieldName)
        {
            int count = 0;
            for (int i = 0; i < result.Writes.Count; i++)
            {
                IlWrite write = result.Writes[i];
                if (write.Field.Name == fieldName && write.Field.DeclaringType == declaringType)
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

        private static void AssertHasWrite(
            IlWriteScanResult result,
            Type declaringType,
            string fieldName,
            string methodName)
        {
            IlWrite write = FindWrite(result, declaringType, fieldName, methodName);
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

        private static IEnumerable<Type> ThrowingTypes()
        {
            yield return typeof(NoWriteTarget);
            throw new InvalidOperationException("type enumeration failed on purpose");
        }

#pragma warning disable 0649 // These fields exist to prove the scanner does not invent writes.
        private sealed class DirectWriteTarget
        {
            public int InstanceValue;
            public static int StaticValue;
            public int ConstructorValue;
            public int NeverWritten;

            public DirectWriteTarget()
            {
                ConstructorValue = 7;
            }

            public void SetInstance(int value)
            {
                InstanceValue = value;
            }

            public static void SetStatic(int value)
            {
                StaticValue = value;
            }
        }

        private sealed class NoWriteTarget
        {
            public int NeverWritten;
            public int Written;

            public void DoNothing()
            {
            }

            public void WriteIt()
            {
                Written = 1;
            }
        }
#pragma warning restore 0649

        private sealed class ExternalFieldHolder
        {
            public int Value;
        }

        private sealed class ExternalFieldWriter
        {
            public void Write(ExternalFieldHolder holder)
            {
                holder.Value = 1;
            }
        }

        private sealed class StaticConstructorTarget
        {
            public static int Initialized = 42;
        }

        private sealed class OperandTarget
        {
            public int Selected;
            public long LongValue;
            public double DoubleValue;
            public float FloatValue;
            public string TextValue;

            public int Decode(int selector, long big, double real, float small, string text)
            {
                switch (selector)
                {
                    case 0:
                        Selected = 1;
                        break;
                    case 1:
                        Selected = 2;
                        break;
                    case 2:
                        Selected = 3;
                        break;
                    case 3:
                        Selected = 4;
                        break;
                    case 4:
                        Selected = 5;
                        break;
                    case 5:
                        Selected = 6;
                        break;
                    case 6:
                        Selected = 7;
                        break;
                    case 7:
                        Selected = 8;
                        break;
                    default:
                        Selected = -1;
                        break;
                }

                LongValue = big + 0x0102030405060708L;
                DoubleValue = real + 1.5d;
                FloatValue = small + 1.25f;
                TextValue = text + "!";
                return Selected;
            }
        }

        private sealed class IndirectWriteTarget
        {
            public int InstanceValue;
            public static int StaticValue;

            public ref int GetInstanceRef()
            {
                return ref InstanceValue;
            }

            public static ref int GetSharedRef()
            {
                return ref StaticValue;
            }
        }

        private sealed class CappedTarget
        {
            public int A;
            public int B;
            public int C;
            public int D;

            public void WriteA()
            {
                A = 1;
            }

            public void WriteB()
            {
                B = 2;
            }

            public void WriteC()
            {
                C = 3;
            }

            public void WriteD()
            {
                D = 4;
            }
        }

        private sealed class MissingBodyTarget
        {
            [DllImport("kernel32.dll")]
            public static extern void NativeCall();

            public void Present()
            {
            }
        }

        private abstract class AbstractTarget
        {
            public int ConcreteValue;

            public abstract void AbstractMethod();

            public void WriteConcrete()
            {
                ConcreteValue = 1;
            }
        }

        private class BaseWriteTarget
        {
            public int BaseValue;

            public void WriteBase()
            {
                BaseValue = 1;
            }
        }

        private sealed class DerivedWriteTarget : BaseWriteTarget
        {
            public int DerivedValue;

            public void WriteDerived()
            {
                DerivedValue = 1;
            }
        }

        private sealed class GenericHolder<T>
        {
            public T Value;

            public void Set(T value)
            {
                Value = value;
            }
        }

        private sealed class GenericBox<T>
        {
            public T Value;
        }

        private sealed class GenericMethodTarget
        {
            public void SetBox<T>(GenericBox<T> box, T value)
            {
                box.Value = value;
            }
        }
    }
}
