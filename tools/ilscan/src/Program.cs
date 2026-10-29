using System;
using System.IO;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace Playtest.IlScan
{
    /// <summary>
    /// <c>Playtest.IlScan --input &lt;il_inputs.json&gt; --output &lt;document.json&gt;</c>. Exit code 0
    /// means the document was written, 2 that the arguments or inputs were invalid, and 1 that the scan
    /// failed. The document records every assembly, type and method body it could not read; a partial
    /// scan is reported as incomplete, never as a smaller complete one.
    /// </summary>
    public static class Program
    {
        private static readonly JsonSerializerOptions Output = new JsonSerializerOptions
        {
            WriteIndented = true,
            Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        };

        public static int Main(string[] args)
        {
            string input = null;
            string output = null;
            for (int i = 0; i < args.Length; i++)
            {
                if (args[i] == "--input" && i + 1 < args.Length && input == null) input = args[++i];
                else if (args[i] == "--output" && i + 1 < args.Length && output == null) output = args[++i];
                else return Usage("unexpected argument '" + args[i] + "'");
            }

            if (input == null || output == null) return Usage("--input and --output are required");
            output = Path.GetFullPath(output);
            if (File.Exists(output) || Directory.Exists(output)) return Usage("the output already exists; choose a new file");

            IlScanInputs inputs;
            try
            {
                inputs = IlScanInputs.Parse(File.ReadAllText(input, Encoding.UTF8));
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is JsonException
                || ex is FormatException)
            {
                return Usage("the inputs could not be read: " + ex.Message);
            }

            string temporary = null;
            try
            {
                string json = IlDiscovery.Collect(inputs).ToJson().ToJsonString(Output) + "\n";
                Directory.CreateDirectory(Path.GetDirectoryName(output));
                temporary = output + "." + Guid.NewGuid().ToString("N") + ".tmp";
                File.WriteAllText(temporary, json, new UTF8Encoding(false));
                File.Move(temporary, output, overwrite: false);
                temporary = null;
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("the IL scan failed: " + ex.GetType().Name + ": " + ex.Message);
                return 1;
            }
            finally
            {
                if (temporary != null) File.Delete(temporary);
            }
        }

        private static int Usage(string problem)
        {
            Console.Error.WriteLine(problem);
            Console.Error.WriteLine("usage: Playtest.IlScan --input <il_inputs.json> --output <document.json>");
            return 2;
        }
    }
}
