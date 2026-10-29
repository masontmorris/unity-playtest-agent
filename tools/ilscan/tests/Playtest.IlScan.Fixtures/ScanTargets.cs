using System.Runtime.InteropServices;

namespace Playtest.IlScan.Fixtures
{
    /// <summary>The scanner's original targets, one behaviour per type.</summary>
    public static class ScanTargets
    {
#pragma warning disable 0649 // These fields exist to prove the scanner does not invent writes.
        public sealed class DirectWriteTarget
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

        public sealed class NoWriteTarget
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

        public sealed class ExternalFieldHolder
        {
            public int Value;
        }

        public sealed class ExternalFieldWriter
        {
            public void Write(ExternalFieldHolder holder)
            {
                holder.Value = 1;
            }
        }

        public sealed class StaticConstructorTarget
        {
            public static int Initialized = 42;
        }

        public sealed class OperandTarget
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

        public sealed class IndirectWriteTarget
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

        public sealed class CappedTarget
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

        public sealed class MissingBodyTarget
        {
            [DllImport("kernel32.dll")]
            public static extern void NativeCall();

            public void Present()
            {
            }
        }

        public abstract class AbstractTarget
        {
            public int ConcreteValue;

            public abstract void AbstractMethod();

            public void WriteConcrete()
            {
                ConcreteValue = 1;
            }
        }

        public class BaseWriteTarget
        {
            public int BaseValue;

            public void WriteBase()
            {
                BaseValue = 1;
            }
        }

        public sealed class DerivedWriteTarget : BaseWriteTarget
        {
            public int DerivedValue;

            public void WriteDerived()
            {
                DerivedValue = 1;
            }
        }

        public sealed class GenericHolder<T>
        {
            public T Value;

            public void Set(T value)
            {
                Value = value;
            }
        }

        public sealed class GenericBox<T>
        {
            public T Value;
        }

        public sealed class GenericMethodTarget
        {
            public void SetBox<T>(GenericBox<T> box, T value)
            {
                box.Value = value;
            }
        }
    }
}
