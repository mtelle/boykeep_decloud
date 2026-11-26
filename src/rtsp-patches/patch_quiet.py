import sys
# Quiet rtspd's log: the SDK sample prints "encode fps:NN" every 2 s for EACH encoder channel,
# by far the biggest writer to /tmp/rtsp.log. Only print it when VENC_FPS_LOG is set (debugging).
p = sys.argv[1]
s = open(p, encoding='latin-1').read()
a = '            ak_print_normal_ex(MODULE_ID_VENC, "encode fps:%d\\n", count * 1000 / (end_t - start_t) );\n'
assert s.count(a) == 1, ("encode fps", s.count(a))
s = s.replace(a, '            if (getenv("VENC_FPS_LOG"))\n    ' + a, 1)
open(p, 'w', encoding='latin-1').write(s)
print("patch quiet OK (encode fps only with VENC_FPS_LOG)")
