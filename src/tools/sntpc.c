/* sntpc.c - minimal SNTP (NTP) client. Queries <server>:123, prints unix time,
 * and with --set sets the system clock (needs root). For de-cloud cameras with no
 * ntpd: sync from the LAN router. Build static for armv5te (uclibc). */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <time.h>
#include <sys/time.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netdb.h>

#define NTP_EPOCH 2208988800UL   /* seconds between 1900 and 1970 */

int main(int argc, char **argv)
{
    if (argc < 2) { fprintf(stderr, "usage: sntpc <server> [--set]\n"); return 2; }
    int doset = 0, i;
    for (i = 2; i < argc; i++) if (!strcmp(argv[i], "--set")) doset = 1;

    struct addrinfo hints, *res;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_INET; hints.ai_socktype = SOCK_DGRAM;
    if (getaddrinfo(argv[1], "123", &hints, &res) != 0) { fprintf(stderr, "resolve fail\n"); return 1; }

    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) { perror("socket"); freeaddrinfo(res); return 1; }
    struct timeval to = { 5, 0 };
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &to, sizeof to);

    unsigned char pkt[48];
    memset(pkt, 0, sizeof pkt);
    pkt[0] = 0x1b;                /* LI=0, VN=3, Mode=3 (client) */
    if (sendto(s, pkt, 48, 0, res->ai_addr, res->ai_addrlen) < 0) { perror("send"); return 1; }
    freeaddrinfo(res);

    int n = recvfrom(s, pkt, 48, 0, NULL, NULL);
    if (n < 48) { fprintf(stderr, "no reply\n"); return 1; }

    unsigned long secs = ((unsigned long)pkt[40] << 24) | ((unsigned long)pkt[41] << 16) |
                         ((unsigned long)pkt[42] << 8)  |  (unsigned long)pkt[43];
    if (secs < NTP_EPOCH) { fprintf(stderr, "bad time\n"); return 1; }
    time_t t = (time_t)(secs - NTP_EPOCH);

    if (doset) {
        struct timeval stv = { t, 0 };
        if (settimeofday(&stv, NULL) != 0) { perror("settimeofday"); return 1; }
    }
    printf("%ld\n", (long)t);
    return 0;
}
