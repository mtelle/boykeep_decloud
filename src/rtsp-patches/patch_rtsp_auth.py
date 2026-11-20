import sys
# Wire up rtsp_server_init_param(rtsp_init_param*) with RTSP_USER/RTSP_PASS from the environment
# (set by start.sh from deploy/auth.env, if that file exists and is filled in). Harmless when
# unset: byte-for-byte the same NULL call as the stock sample.
#
# CONFIRMED ON HARDWARE: this does NOT actually restrict access to vs0/vs1. librtsp_server.so has
# the Digest-auth machinery (MD5Auth_*, a literal "WWW-Authenticate: Digest" string), and
# ak_rtsp_server.h's own (translated) comments say cRecordUrlPrefix is the live-stream URL prefix
# and cLiveStearmUrlPrefix is the recording prefix - backwards from their names. Tried both: only
# cRecordUrlPrefix set to "vs", then both fields set. Neither gated vs0/vs1 - VLC connects and
# plays with zero credentials every time, even though the log confirms the struct was built and
# rtsp_server_init_param() was called with it. The Digest-auth code is present in the .so but does
# not appear to be wired to the live-stream request handler in this particular build. See
# docs/13-auth.md. Left in place (inert from a security standpoint, but harmless) in case it's
# ever worth revisiting - e.g. if a differently-built librtsp_server.so surfaces.
p = sys.argv[1]
s = open(p, encoding="latin-1").read()

old = "    rtsp_server_init_param(NULL);\n"
new = (
    "    {\n"
    "        const char *ru = getenv(\"RTSP_USER\");\n"
    "        const char *rp = getenv(\"RTSP_PASS\");\n"
    "        if (ru && *ru && rp && *rp) {\n"
    "            static rtsp_init_param auth_param;\n"
    "            memset(&auth_param, 0, sizeof(auth_param));\n"
    "            auth_param.iServerPort = 554;\n"
    "            snprintf(auth_param.cUserName, sizeof(auth_param.cUserName), \"%s\", ru);\n"
    "            snprintf(auth_param.cPassword, sizeof(auth_param.cPassword), \"%s\", rp);\n"
    "            /* Both fields set to \"vs\" (ak_rtsp_server.h names them backwards from what\n"
    "               they do - see this file's own header comment). Confirmed on hardware that\n"
    "               neither this nor either field alone actually gates vs0/vs1 - see docs/13-auth.md. */\n"
    "            snprintf(auth_param.cRecordUrlPrefix, sizeof(auth_param.cRecordUrlPrefix), \"vs\");\n"
    "            snprintf(auth_param.cLiveStearmUrlPrefix, sizeof(auth_param.cLiveStearmUrlPrefix), \"vs\");\n"
    "            printf(\"RTSP: credentials set for user '%s' (NOTE: confirmed NOT to gate vs0/vs1 on this SDK build - see docs/13-auth.md)\\n\", ru);\n"
    "            rtsp_server_init_param(&auth_param);\n"
    "        } else {\n"
    "            rtsp_server_init_param(NULL);\n"
    "        }\n"
    "    }\n"
)
assert s.count(old) == 1, s.count(old)
s = s.replace(old, new, 1)

open(p, "w", encoding="latin-1").write(s)
print("patch rtsp_auth OK")
