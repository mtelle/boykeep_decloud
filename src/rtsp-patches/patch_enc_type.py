import sys,re
p=sys.argv[1]; s=open(p).read()
a0='            vs->enc_out_type = 1;\n            vs->profile = PROFILE_JPEG;\n        }\n        '
x0=(a0+
    '{ int _ov=aas_ini_get_int(ini,"video0","enc_type",-1); if(_ov>=0){vs->enc_out_type=_ov; printf("OVERRIDE0 enc=%d ",_ov);} }\n        '
    '{ int _op=aas_ini_get_int(ini,"video0","profile_type",-1); if(_op>=0){vs->profile=_op; printf("OVERRIDE0 prof=%d ",_op);} }\n        ')
a1='            vs1->enc_out_type = 1;\n            vs1->profile = PROFILE_JPEG;\n        }\n        '
x1=(a1+
    '{ int _ov=aas_ini_get_int(ini,"video1","enc_type",-1); if(_ov>=0){vs1->enc_out_type=_ov; printf("OVERRIDE1 enc=%d ",_ov);} }\n        '
    '{ int _op=aas_ini_get_int(ini,"video1","profile_type",-1); if(_op>=0){vs1->profile=_op; printf("OVERRIDE1 prof=%d ",_op);} }\n        ')
assert s.count(a0)==1 and s.count(a1)==1,(s.count(a0),s.count(a1))
s=s.replace(a0,x0,1).replace(a1,x1,1)
# Drop ONLY the MP3/AMR codec registration (no libs for those). KEEP the AAC registration:
# AENC_OpenAac / AENC_Aac_GetVersionInfo are provided by libakaac.so (shipped to the camera),
# and ak_aenc_registerencoder() must run for the AAC case so the mic stream actually encodes.
s=re.sub(r'AENC_Open(Mp3|Amr)','0',s)
s=re.sub(r'AENC_(Mp3|Amr)_GetVersionInfo','0',s)
open(p,'w').write(s); print("patch enc_type OK (AAC kept, +profile_type override)")
