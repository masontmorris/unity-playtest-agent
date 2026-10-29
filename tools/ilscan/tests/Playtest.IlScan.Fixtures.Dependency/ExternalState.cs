namespace Playtest.IlScan.Fixtures.Dependency
{
    /// <summary>Fields declared outside the scanned assembly.</summary>
    public sealed class ExternalState
    {
        public int Level;
        public static int Total;

        public sealed class Part
        {
            public int Weight;
        }
    }

    public sealed class ExternalGeneric<T>
    {
        public T Item;
    }
}
