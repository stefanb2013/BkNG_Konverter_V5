// Converts Ladder Diagrams in the format of Automation Studio < 6 (<networks> ... </networks>)
// into the PLCopen format of Automation Studio 6, using the libraries of an installed AS6.
//
// Automation Studio 6 does this itself when a file is opened and saved in its editor; this
// program calls the same steps without the editor:
//   1. BR.AS.Iec.GraphicalEditor.Data.LdConversion.ConversionEngine  - read the old file
//   2. BR.AS.Iec.GraphicalEditor.CodeAnalysis.CodeGenerator          - generate the code (CodeInformation)
//   3. BR.AS.Iec.GraphicalEditor.Data.FileHandling.FileSaveEngine    - write the AS6 file with checksum
// The classes are internal to Automation Studio; they are called by reflection so that this
// program does not depend on one AS6 build.  It is compiled by bkng_converter.py with the C#
// compiler of the .NET Framework (csc.exe) as a 32-bit program, like Automation Studio.
//
// Usage: LdConvert.exe <AS6 bin folder> <working version, e.g. 6.7> <list file>
//   Each line of the list file is "<source>|<target>" (both UTF-8).  For each line one line
//   "OK <source>" or "ERROR <source>\t<message>" is written to stdout.
//   Exit code: 0 all converted, 2 some files failed, 1 the AS6 libraries could not be used.
using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;

static class LdConvert
{
    static string bin;

    static int Main(string[] args)
    {
        if (args.Length != 3)
        {
            Console.Error.WriteLine("Usage: LdConvert.exe <AS6 bin folder> <working version> <list file>");
            return 1;
        }
        bin = args[0];
        AppDomain.CurrentDomain.AssemblyResolve += Resolve;
        Converter converter;
        try
        {
            converter = new Converter(bin, args[1]);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(Unwrap(ex).GetType().Name + ": " + Unwrap(ex).Message);
            return 1;
        }
        Console.OutputEncoding = new UTF8Encoding(false);
        int failed = 0;
        foreach (var line in File.ReadAllLines(args[2], Encoding.UTF8).Where(l => l.Contains("|")))
        {
            var parts = line.Split('|');
            try
            {
                converter.Convert(parts[0], parts[1]);
                Console.WriteLine("OK " + parts[0]);
            }
            catch (Exception ex)
            {
                failed++;
                var error = Unwrap(ex);
                Console.WriteLine("ERROR " + parts[0] + "\t" + error.GetType().Name + ": "
                                  + error.Message.Replace("\r", " ").Replace("\n", " "));
            }
        }
        return failed == 0 ? 0 : 2;
    }

    static Exception Unwrap(Exception ex)
    {
        while (ex is TargetInvocationException && ex.InnerException != null) ex = ex.InnerException;
        return ex;
    }

    // The referenced assemblies of Automation Studio are loaded from its bin folder.
    static Assembly Resolve(object sender, ResolveEventArgs e)
    {
        var name = new AssemblyName(e.Name).Name;
        var loaded = AppDomain.CurrentDomain.GetAssemblies().FirstOrDefault(a => a.GetName().Name == name);
        if (loaded != null) return loaded;
        var path = Path.Combine(bin, name + ".dll");
        return File.Exists(path) ? Assembly.LoadFrom(path) : null;
    }

    class Converter
    {
        const BindingFlags Instance = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
        readonly Type conversionType, saveType;
        readonly MethodInfo load, generate, save;
        readonly object workingVersion;

        public Converter(string bin, string version)
        {
            var data = Assembly.LoadFrom(Path.Combine(bin, "BR.AS.Iec.GraphicalEditor.Data.dll"));
            var analysis = Assembly.LoadFrom(Path.Combine(bin, "BR.AS.Iec.GraphicalEditor.CodeAnalysis.dll"));
            conversionType = data.GetType("BR.AS.Iec.GraphicalEditor.Data.LdConversion.ConversionEngine", true);
            saveType = data.GetType("BR.AS.Iec.GraphicalEditor.Data.FileHandling.FileSaveEngine", true);
            load = conversionType.GetMethod("LoadSourceFile", Instance);
            save = saveType.GetMethod("GetSourceFileContent", Instance);
            generate = analysis.GetType("BR.AS.Iec.GraphicalEditor.CodeAnalysis.CodeGenerator", true)
                               .GetMethod("Generate", BindingFlags.Public | BindingFlags.Static);
            if (load == null || save == null || generate == null)
                throw new MissingMethodException("The ladder conversion of this Automation Studio is not supported.");
            workingVersion = CreateVersion(generate.GetParameters()[1].ParameterType, version);
        }

        public void Convert(string source, string target)
        {
            // A new engine per file: they keep the state of the file they work on.
            var conversion = Activator.CreateInstance(conversionType, true);
            var saver = Activator.CreateInstance(saveType, true);
            var content = File.ReadAllText(source, Encoding.UTF8);
            var model = load.Invoke(conversion, new object[] { content, false, new FileInfo(source) });
            var result = generate.Invoke(null, new[] { model, workingVersion, (object)true });
            var code = (string)result.GetType().GetProperty("Code").GetValue(result, null);
            var lineMapInfos = result.GetType().GetProperty("LineMapInfos").GetValue(result, null);
            var text = (string)save.Invoke(saver, new[] { model, code, lineMapInfos });
            File.WriteAllText(target, text, new UTF8Encoding(true));
        }

        static object CreateVersion(Type versionType, string version)
        {
            var parse = versionType.GetMethod("Parse", BindingFlags.Public | BindingFlags.Static, null,
                                              new[] { typeof(string) }, null);
            if (parse != null) return parse.Invoke(null, new object[] { version });
            var ctor = versionType.GetConstructor(new[] { typeof(string) });
            if (ctor != null) return ctor.Invoke(new object[] { version });
            var parts = version.Split('.').Select(int.Parse).ToArray();
            ctor = versionType.GetConstructors().First(c => c.GetParameters().Length == parts.Length
                                                           && c.GetParameters().All(p => p.ParameterType == typeof(int)));
            return ctor.Invoke(parts.Cast<object>().ToArray());
        }
    }
}
