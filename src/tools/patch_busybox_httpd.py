import sys
# BusyBox 1.36.1 httpd, two changes for onvif_simple_server:
#  1. treat /onvif/* as CGI (as documented upstream);
#  2. never ask HTTP Basic auth for /onvif/*: ONVIF clients (Home Assistant, ODM, NVRs) send their
#     credentials inside the SOAP body (WS-Security UsernameToken), never as an HTTP header, so a
#     "/:user:pass" line in httpd.conf answered every ONVIF request with 401. onvif_simple_server
#     checks those credentials itself (user=/password= in its conf, set by start.sh).
p = sys.argv[1]
s = open(p).read()
anchor = '''	if (is_prefixed_with(tptr, "cgi-bin/")) {
		if (tptr[8] == '\\0') {
			/* protect listing "cgi-bin/" */
			send_headers_and_exit(HTTP_FORBIDDEN);
		}
		cgi_type = CGI_NORMAL;
	}
'''
add = '''	else if (is_prefixed_with(tptr, "onvif/")) {
		if (tptr[6] == '\\0') {
			/* protect listing "onvif/" */
			send_headers_and_exit(HTTP_FORBIDDEN);
		}
		cgi_type = CGI_NORMAL;
	}
'''
assert s.count(anchor) == 1, ("anchor", s.count(anchor))
s = s.replace(anchor, anchor + add, 1)

auth_anchor = '''static int check_user_passwd(const char *path, char *user_and_passwd)
{
	Htaccess *cur;
	const char *prev = NULL;
'''
auth_add = '''
	/* /onvif/* authenticates itself (WS-Security in the SOAP body, checked by
	 * onvif_simple_server): ONVIF clients never send HTTP Basic credentials. */
	if (is_prefixed_with(path, "/onvif/"))
		return 1;
'''
assert s.count(auth_anchor) == 1, ("auth_anchor", s.count(auth_anchor))
s = s.replace(auth_anchor, auth_anchor + auth_add, 1)
open(p, 'w').write(s)
print("patch busybox httpd OK (/onvif CGI + own auth)")
