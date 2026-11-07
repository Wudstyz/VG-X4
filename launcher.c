/* Lanceur de « Atelier Traceuse.app » : démarre le Python embarqué dans Contents/Resources/python
   (aucune dépendance à Homebrew ni à un Python installé sur le Mac). */
#include <Python.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    char exe[PATH_MAX], real[PATH_MAX]; uint32_t n = sizeof(exe);
    _NSGetExecutablePath(exe, &n); realpath(exe, real);
    char macos[PATH_MAX]; strcpy(macos, dirname(real));
    char script[PATH_MAX]; snprintf(script, sizeof(script), "%s/../Resources/app.py", macos);
    char home[PATH_MAX]; snprintf(home, sizeof(home), "%s/../Resources/python", macos);
    char home_real[PATH_MAX];
    setenv("PYTHONHOME", realpath(home, home_real) ? home_real : home, 1);
    char path[4096]; snprintf(path, sizeof(path), "/opt/homebrew/bin:/usr/local/bin:%s", getenv("PATH") ? getenv("PATH") : "");
    setenv("PATH", path, 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);   /* ne pas modifier le bundle signé */
    setenv("PYTHONNOUSERSITE", "1", 1);          /* ignorer les paquets Python de l'utilisateur */
    char *args[] = { real, script, NULL };
    return Py_BytesMain(2, args);
}
