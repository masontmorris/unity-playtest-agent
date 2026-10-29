// SPDX-License-Identifier: Apache-2.0
using System.IO;
using System.Text;
using NUnit.Framework;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Tests
{
    [TestFixture]
    public sealed class JsonLineConnectionTests
    {
        [Test]
        public void WriteThenRead_RoundTrips()
        {
            var connection = new JsonLineConnection(new MemoryStream(), 1024);
            connection.WriteLine("{\"a\":1}");
            connection.Stream.Position = 0;

            Assert.That(connection.ReadLine(), Is.EqualTo("{\"a\":1}"));
            Assert.That(connection.ReadLine(), Is.Null);
        }

        [Test]
        public void ReadLine_HandlesCrLfAndSeveralLines()
        {
            var connection = new JsonLineConnection(
                new MemoryStream(Encoding.UTF8.GetBytes("one\r\ntwo\nthree")),
                64);
            Assert.That(connection.ReadLine(), Is.EqualTo("one"));
            Assert.That(connection.ReadLine(), Is.EqualTo("two"));
            Assert.That(connection.ReadLine(), Is.EqualTo("three"), "a final unterminated line is still a line");
            Assert.That(connection.ReadLine(), Is.Null);
        }

        [Test]
        public void ReadLine_DoesNotLoseBytesAfterNewline()
        {
            var connection = new JsonLineConnection(
                new MemoryStream(Encoding.UTF8.GetBytes("first\nsecond\n")),
                64);
            Assert.That(connection.ReadLine(), Is.EqualTo("first"));
            Assert.That(connection.ReadLine(), Is.EqualTo("second"));
        }

        [Test]
        public void ReadLine_RejectsOversizedLine()
        {
            var connection = new JsonLineConnection(
                new MemoryStream(Encoding.UTF8.GetBytes(new string('x', 33) + "\n")),
                32);
            Assert.Throws<InvalidDataException>(() => connection.ReadLine());
        }

        [Test]
        public void ReadLine_AcceptsLineOfExactlyMaxBytes()
        {
            var connection = new JsonLineConnection(
                new MemoryStream(Encoding.UTF8.GetBytes(new string('x', 32) + "\n")),
                32);
            Assert.That(connection.ReadLine(), Has.Length.EqualTo(32));
        }

        [Test]
        public void ReadLine_RejectsInvalidUtf8()
        {
            var connection = new JsonLineConnection(
                new MemoryStream(new byte[] { 0xff, 0xfe, (byte)'\n' }),
                64);
            Assert.Throws<DecoderFallbackException>(() => connection.ReadLine());
        }

        [Test]
        public void WriteLine_RejectsEmbeddedNewline()
        {
            var connection = new JsonLineConnection(new MemoryStream(), 64);
            Assert.Throws<System.ArgumentException>(() => connection.WriteLine("a\nb"));
            Assert.Throws<System.ArgumentException>(() => connection.WriteLine("a\rb"));
        }
    }

    [TestFixture]
    public sealed class BridgeTokenTests
    {
        [Test]
        public void Create_IsLowercaseHexWithRequestedEntropy()
        {
            string token = BridgeToken.Create();
            Assert.That(token, Has.Length.EqualTo(64));
            Assert.That(token, Does.Match("^[0-9a-f]{64}$"));

            string shortToken = BridgeToken.Create(16);
            Assert.That(shortToken, Has.Length.EqualTo(32));
        }

        [Test]
        public void Create_IsRandomAcrossCalls()
        {
            Assert.That(BridgeToken.Create(), Is.Not.EqualTo(BridgeToken.Create()));
        }

        [Test]
        public void Create_RefusesLowEntropy()
        {
            Assert.Throws<System.ArgumentOutOfRangeException>(() => BridgeToken.Create(8));
        }

        [Test]
        public void Matches_OnlyMatchesExactNonEmptyStrings()
        {
            Assert.That(BridgeToken.Matches("abc", "abc"), Is.True);
            Assert.That(BridgeToken.Matches("abc", "abd"), Is.False);
            Assert.That(BridgeToken.Matches("abc", "abcd"), Is.False);
            Assert.That(BridgeToken.Matches(null, null), Is.False);
            Assert.That(BridgeToken.Matches("abc", null), Is.False);
        }
    }
}
