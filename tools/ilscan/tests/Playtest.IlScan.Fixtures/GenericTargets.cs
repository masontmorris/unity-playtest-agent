using System;
using System.Collections;
using System.Collections.Generic;

namespace Playtest.IlScan.Fixtures
{
    // Code shapes whose field tokens need a generic context, a member reference or another assembly.
    // Live reflection and inspection-only loading must name each of these fields with the same id.

    /// <summary>Stores inside a generic type reach its fields through an instantiation over its own parameters.</summary>
    public class GenericOwner<T>
    {
        public T Value;
        public static T Shared;
        public volatile int Flag;

        public void Set(T value)
        {
            Value = value;
            Flag = 1;
        }

        public static void SetShared(T value)
        {
            Shared = value;
        }

        public ref T Address()
        {
            return ref Value;
        }
    }

    /// <summary>The self-registering base class many projects use.</summary>
    public abstract class SingletonBase<T> where T : class
    {
        private static T s_instance;

        public static T Instance => s_instance;

        protected void Register(T self)
        {
            s_instance = self;
        }
    }

    public sealed class SingletonUser : SingletonBase<SingletonUser>
    {
        public int Started;

        public void Begin()
        {
            Register(this);
            Started = 1;
        }
    }

    /// <summary>Nested types of a generic type are generic too, with the outer parameters first.</summary>
    public class Outer<T>
    {
        public class Inner
        {
            public T Item;

            public void Put(T item)
            {
                Item = item;
            }
        }

        public class Pair<U>
        {
            public T First;
            public U Second;

            public void Assign(T first, U second)
            {
                First = first;
                Second = second;
            }
        }
    }

    public class BaseHolder<T>
    {
        public T Held;
    }

    /// <summary>A derived generic type writes a base field through an instantiation over its own parameter.</summary>
    public class DerivedHolder<U> : BaseHolder<U>
    {
        public void Hold(U value)
        {
            Held = value;
        }
    }

    /// <summary>Non-generic code writes fields of closed instantiations.</summary>
    public sealed class ClosedWriter
    {
        public void Write(GenericOwner<int> owner, Outer<string>.Pair<float> pair)
        {
            owner.Value = 3;
            pair.Second = 2f;
            GenericOwner<string>.Shared = "shared";
        }
    }

    /// <summary>A generic method writes through an instantiation over the method's parameter.</summary>
    public sealed class MethodContext
    {
        public int Calls;

        public void Store<T>(GenericOwner<T> owner, T value)
        {
            owner.Value = value;
            Calls++;
        }

        public void Swap<T>(ref T left, ref T right, List<T>[] lists, Dictionary<string, T> named)
        {
            T kept = left;
            left = right;
            right = kept;
            Calls += lists.Length + named.Count;
        }

        public void Accept(List<int>[] lists, ref List<string> names, out KeyValuePair<int, float> entry)
        {
            entry = default(KeyValuePair<int, float>);
            Calls = lists.Length + names.Count;
        }
    }

    /// <summary>Compiler-generated closures, iterators and tuples.</summary>
    public sealed class Generated<T>
    {
        public int Counter;
        public (int, float) Pair;

        public Action Capture(T seed)
        {
            T local = seed;
            return () =>
            {
                Counter++;
                local = default(T);
            };
        }

        public IEnumerable<T> Repeat(T item, int count)
        {
            for (int i = 0; i < count; i++)
            {
                yield return item;
            }
        }

        public IEnumerator Wait(int frames)
        {
            while (frames-- > 0)
            {
                Counter = frames;
                yield return null;
            }
        }

        public void WriteTuple()
        {
            Pair.Item1 = 3;
        }
    }

    public struct Point
    {
        public int X;
        public int Y;
    }

    /// <summary>A field of a struct written through the address of the containing field.</summary>
    public sealed class PointHolder
    {
        public Point Location;
        public static Point Origin;

        public void MoveX(int x)
        {
            Location.X = x;
            Origin.Y = x;
        }
    }

    /// <summary>A delegate's methods are implemented by the runtime and have no IL body.</summary>
    public delegate void Notify(int value);
}
