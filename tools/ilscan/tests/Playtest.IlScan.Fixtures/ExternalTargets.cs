using Playtest.IlScan.Fixtures.Dependency;

namespace Playtest.IlScan.Fixtures
{
    /// <summary>Writes to fields declared in another assembly, which must be among the references.</summary>
    public sealed class ExternalWriter
    {
        public int Local;

        public void Write(ExternalState state, ExternalGeneric<int> generic)
        {
            state.Level = 2;
            generic.Item = 4;
            ExternalState.Total = 5;
            Local = 1;
        }

        public void Nested(ExternalState.Part part)
        {
            part.Weight = 3;
        }

        public static void Reset()
        {
            ExternalState.Total = 0;
        }
    }
}
