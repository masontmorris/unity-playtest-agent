using System;
using System.IO;
using System.Text;

namespace Playtest.Bridge.Transport
{
    /// <summary>
    /// One JSON object per line, UTF-8, <c>\n</c> terminated. A line may use <c>\r\n</c>.
    /// Reads are bounded so a peer cannot grow the buffer without limit. Bytes after a newline
    /// stay buffered for the next call.
    /// </summary>
    /// <remarks>
    /// One thread may read while another writes; the instance owns no stream lifetime.
    /// </remarks>
    public sealed class JsonLineConnection
    {
        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);

        private readonly Stream _stream;
        private readonly int _maxBytes;
        private readonly byte[] _buffer;
        private int _start;
        private int _end;
        private bool _eof;

        public JsonLineConnection(Stream stream, int maxBytes)
        {
            _stream = stream ?? throw new ArgumentNullException(nameof(stream));
            if (maxBytes <= 0)
            {
                throw new ArgumentOutOfRangeException(nameof(maxBytes));
            }

            _maxBytes = maxBytes;
            _buffer = new byte[Math.Min(Math.Max(maxBytes + 1, 256), 8192)];
        }

        public Stream Stream
        {
            get { return _stream; }
        }

        /// <summary>
        /// Returns the next line without its terminator, or null on EOF or timeout. A final line
        /// without a terminator is returned as-is. Oversized or non-UTF-8 input throws
        /// <see cref="InvalidDataException"/> / <see cref="DecoderFallbackException"/>.
        /// </summary>
        public string ReadLine(int timeoutMs = 0)
        {
            var line = new MemoryStream();
            while (true)
            {
                if (_start < _end)
                {
                    int newline = Array.IndexOf(_buffer, (byte)'\n', _start, _end - _start);
                    if (newline >= 0)
                    {
                        Append(line, _start, newline - _start);
                        _start = newline + 1;
                        return Decode(line, stripCarriageReturn: true);
                    }

                    Append(line, _start, _end - _start);
                    _start = _end;
                }

                if (_eof)
                {
                    return line.Length == 0 ? null : Decode(line, stripCarriageReturn: false);
                }

                if (!Fill(timeoutMs))
                {
                    return null;
                }
            }
        }

        /// <summary>Writes one line and flushes it. Embedded CR or LF is a caller bug.</summary>
        public void WriteLine(string line)
        {
            if (line == null)
            {
                throw new ArgumentNullException(nameof(line));
            }

            if (line.IndexOf('\n') >= 0 || line.IndexOf('\r') >= 0)
            {
                throw new ArgumentException("a JSONL line cannot contain CR or LF", nameof(line));
            }

            byte[] bytes = Encoding.UTF8.GetBytes(line + "\n");
            _stream.Write(bytes, 0, bytes.Length);
            _stream.Flush();
        }

        private bool Fill(int timeoutMs)
        {
            if (_start > 0)
            {
                int remaining = _end - _start;
                if (remaining > 0)
                {
                    Buffer.BlockCopy(_buffer, _start, _buffer, 0, remaining);
                }

                _end = remaining;
                _start = 0;
            }

            ConfigureTimeout(timeoutMs);
            try
            {
                int read = _stream.Read(_buffer, _end, _buffer.Length - _end);
                if (read <= 0)
                {
                    _eof = true;
                    return true;
                }

                _end += read;
                return true;
            }
            catch (IOException) when (timeoutMs > 0 && _stream.CanTimeout)
            {
                return false;
            }
        }

        private void ConfigureTimeout(int timeoutMs)
        {
            if (!_stream.CanTimeout)
            {
                return;
            }

            try
            {
                _stream.ReadTimeout = timeoutMs;
            }
            catch (InvalidOperationException)
            {
            }
            catch (NotSupportedException)
            {
            }
            catch (ArgumentOutOfRangeException)
            {
            }
        }

        private void Append(MemoryStream line, int offset, int count)
        {
            if (count <= 0)
            {
                return;
            }

            if (line.Length + count > _maxBytes)
            {
                throw new InvalidDataException($"JSONL line exceeds {_maxBytes} bytes");
            }

            line.Write(_buffer, offset, count);
        }

        private static string Decode(MemoryStream line, bool stripCarriageReturn)
        {
            int length = (int)line.Length;
            byte[] bytes = line.GetBuffer();
            if (stripCarriageReturn && length > 0 && bytes[length - 1] == (byte)'\r')
            {
                length--;
            }

            return StrictUtf8.GetString(bytes, 0, length);
        }
    }
}
