import sys
# patch_audio.py - audio DISABLED for live+SDP (keeps tAencMux/tee/override plumbing for later).
#
# Confirmed on-device: the software AAC codec (libakaudiocodec.so) refuses this chip with
# "## ERROR: this is not CHIP(%d)" (a /dev/mem register check baked into the licensed codec).
# ak_aenc_open() still returns AK_SUCCESS despite the internal failure, so init_aenc() never
# sets audio_enable=0 itself; the real failure only shows up later, in audio_get_frame_encode's
# first ak_aenc_send_frame() call, in a path that does NOT reset audio_enable. If we leave this
# alone the RTSP SDP keeps advertising an audio track that will never deliver data, which is the
# documented "stalls after one frame" failure mode. So: skip init_ai/init_aenc/the two audio
# threads AND their later ak_thread_join() calls entirely, and force audio_enable=0 up front ->
# clean video-only SDP, no dead thread, no uninitialised-pthread_t join, no per-boot
# "aenc AAC open error" log spam. Revert this patch (or swap for an open AAC encoder) if audio
# is revisited later.
p = sys.argv[1]
s = open(p, encoding='latin-1').read()

# 1) provide tAencMux (8 pointers = 32 bytes, zero-initialised like the original BSS object).
#    Kept even with audio disabled: cheap, harmless, and needed again the moment audio comes back.
sym_anchor = '#include "aas_ini_parser.h"\n'
assert s.count(sym_anchor) == 1, ("aenc sym anchor", s.count(sym_anchor))
s = s.replace(sym_anchor, sym_anchor +
    '\nvoid *tAencMux[8] = {0};   /* decloud: libmpi_aenc needs it; camera libplat_common lacks it */\n', 1)

# 2) disable audio before it starts: skip init_ai/init_aenc + the two audio threads + their later
#    joins, force audio_enable=0 so the RTSP SDP never advertises an audio track. rtsp_server_init()
#    stays exactly where it was.
anchor = ('    signal (SIGPIPE, SIG_IGN);\n'
          '    init_ai(&rtsp_param.audio);\n'
          '    init_aenc(&rtsp_param.audio);\n'
          '\n'
          '    ak_pthread_t encode_tid;\n'
          '    ak_pthread_t get_tid;\n'
          '\n'
          '    flag = 1;\n'
          '    int chn = 2;\n'
          '    ak_thread_create(&(get_tid), audio_get_stream, (void*)chn, ANYKA_THREAD_MIN_STACK_SIZE, 19);\n'
          '    ak_thread_create(&(encode_tid), audio_get_frame_encode, NULL, ANYKA_THREAD_MIN_STACK_SIZE, 19);\n'
          '    rtsp_server_init();\n')
assert s.count(anchor) == 1, ("audio-disable anchor", s.count(anchor))
replacement = ('    signal (SIGPIPE, SIG_IGN);\n'
               '    audio_enable = 0;   /* decloud: AAC codec rejects this chip (CHIP-id check); no audio */\n'
               '    printf("AUDIO DISABLED (codec chip check failed on this board)\\n");\n'
               '    rtsp_server_init();\n')
s = s.replace(anchor, replacement, 1)

join_old = ('    ak_thread_join(get_tid);\n'
            '    ak_thread_join(encode_tid);\n')
assert s.count(join_old) == 1, ("join anchor", s.count(join_old))
s = s.replace(join_old, '    /* decloud: no audio threads were created, nothing to join */\n', 1)

open(p, 'w', encoding='latin-1').write(s)
print("patch audio (DISABLED: chip-check incompatible; tAencMux kept for later) OK")
