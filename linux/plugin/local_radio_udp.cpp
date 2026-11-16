// ETS2/ATS Local Radio - Linux telemetry plugin
// SPDX-License-Identifier: MPL-2.0
//
// A deliberately tiny SCS telemetry plugin. It reports only what Local Radio
// needs (game id, truck world position, pause state) as one JSON datagram over
// loopback UDP, a few times per second.
//
// Why UDP instead of the upstream shared-memory plugin: under Proton the
// upstream plugin's named file mapping lives inside wineserver and is invisible
// to native Linux programs. Wine's Winsock is backed by real Linux sockets, so a
// datagram sent from inside the game reaches a native server directly, with no
// helper process inside the Wine prefix. The same source builds as:
//   * local-radio-linux.dll  -> bin/win_x64/plugins   (Proton / TruckersMP)
//   * local-radio-linux.so   -> bin/linux_x64/plugins (native Linux game)
//
// Environment overrides (read once at init; Wine passes the Linux env through):
//   LOCAL_RADIO_UDP_PORT   destination port on 127.0.0.1 (default 8331)

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#  include <winsock2.h>
#  include <windows.h>
typedef SOCKET sock_t;
#  define SOCK_INVALID INVALID_SOCKET
#  define EXPORT extern "C" __declspec(dllexport)
static unsigned long long now_ms() { return GetTickCount64(); }
static void close_sock(sock_t s) { closesocket(s); }
#else
#  include <arpa/inet.h>
#  include <netinet/in.h>
#  include <sys/socket.h>
#  include <time.h>
#  include <unistd.h>
typedef int sock_t;
#  define SOCK_INVALID (-1)
#  define EXPORT extern "C" __attribute__((visibility("default")))
static unsigned long long now_ms() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (unsigned long long)ts.tv_sec * 1000ull + (unsigned long long)ts.tv_nsec / 1000000ull;
}
static void close_sock(sock_t s) { close(s); }
#endif

#include "scssdk_telemetry.h"
#include "eurotrucks2/scssdk_eut2.h"
#include "eurotrucks2/scssdk_telemetry_eut2.h"
#include "amtrucks/scssdk_ats.h"
#include "amtrucks/scssdk_telemetry_ats.h"
#include "common/scssdk_telemetry_common_configs.h"
#include "common/scssdk_telemetry_common_channels.h"
#include "common/scssdk_telemetry_truck_common_channels.h"

#define PLUGIN_PROTO 1
#define SEND_INTERVAL_MS 250ull

static sock_t g_sock = SOCK_INVALID;
static struct sockaddr_in g_dest;
static scs_log_t g_log = NULL;

static const char *g_game = "none"; // "ets2" | "ats"
static scs_value_dvector_t g_pos = {0.0, 0.0, 0.0};
static bool g_have_pos = false;
static bool g_paused = true;
static unsigned long long g_last_send = 0;
static unsigned long long g_seq = 0;

static void log_msg(const char *msg) {
    if (g_log) g_log(SCS_LOG_TYPE_message, msg);
}

static void send_state(const char *game_override) {
    if (g_sock == SOCK_INVALID) return;
    char buf[256];
    int n = snprintf(buf, sizeof buf,
                     "{\"v\":%d,\"seq\":%llu,\"game\":\"%s\",\"paused\":%s,\"valid\":%s,"
                     "\"x\":%.3f,\"y\":%.3f,\"z\":%.3f}",
                     PLUGIN_PROTO, ++g_seq, game_override ? game_override : g_game,
                     g_paused ? "true" : "false", g_have_pos ? "true" : "false",
                     g_pos.x, g_pos.y, g_pos.z);
    if (n <= 0 || n >= (int)sizeof buf) return;
    // Fire and forget: if nobody listens, the datagram is simply dropped.
    sendto(g_sock, buf, n, 0, (const struct sockaddr *)&g_dest, sizeof g_dest);
}

static SCSAPI_VOID on_frame_end(const scs_event_t, const void *const, const scs_context_t) {
    unsigned long long t = now_ms();
    if (t - g_last_send >= SEND_INTERVAL_MS) {
        g_last_send = t;
        send_state(NULL);
    }
}

static SCSAPI_VOID on_pause_change(const scs_event_t event, const void *const, const scs_context_t) {
    g_paused = (event == SCS_TELEMETRY_EVENT_paused);
    send_state(NULL); // report pause/resume immediately
}

static SCSAPI_VOID on_world_placement(const scs_string_t, const scs_u32_t, const scs_value_t *const value,
                                      const scs_context_t) {
    if (!value) return; // game reports "no value" e.g. while no truck is spawned
    g_pos = value->value_dplacement.position;
    g_have_pos = true;
}

EXPORT SCSAPI_RESULT scs_telemetry_init(const scs_u32_t version, const scs_telemetry_init_params_t *const params) {
    if (version != SCS_TELEMETRY_VERSION_1_00 && version != SCS_TELEMETRY_VERSION_1_01) {
        return SCS_RESULT_unsupported;
    }
    const scs_telemetry_init_params_v100_t *const p =
        static_cast<const scs_telemetry_init_params_v100_t *>(params);
    g_log = p->common.log;

    if (strcmp(p->common.game_id, SCS_GAME_ID_EUT2) == 0) {
        g_game = "ets2";
    } else if (strcmp(p->common.game_id, SCS_GAME_ID_ATS) == 0) {
        g_game = "ats";
    } else {
        g_game = "unknown";
    }

#ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        log_msg("[local-radio-linux] WSAStartup failed");
        return SCS_RESULT_generic_error;
    }
#endif
    g_sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (g_sock == SOCK_INVALID) {
        log_msg("[local-radio-linux] could not create UDP socket");
#ifdef _WIN32
        WSACleanup();
#endif
        return SCS_RESULT_generic_error;
    }

    int port = 8331;
    const char *env_port = getenv("LOCAL_RADIO_UDP_PORT");
    if (env_port && *env_port) {
        // Hand-rolled instead of atoi/strtol: glibc >= 2.38 redirects those to
        // __isoc23_strtol, which would stop the prebuilt .so loading on older distros.
        long v = 0;
        const char *c = env_port;
        while (*c >= '0' && *c <= '9' && v < 65536) v = v * 10 + (*c++ - '0');
        if (*c == '\0' && v > 0 && v < 65536) port = (int)v;
    }
    memset(&g_dest, 0, sizeof g_dest);
    g_dest.sin_family = AF_INET;
    g_dest.sin_port = htons((unsigned short)port);
    g_dest.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

    bool ok = p->register_for_event(SCS_TELEMETRY_EVENT_frame_end, on_frame_end, NULL) == SCS_RESULT_ok &&
              p->register_for_event(SCS_TELEMETRY_EVENT_paused, on_pause_change, NULL) == SCS_RESULT_ok &&
              p->register_for_event(SCS_TELEMETRY_EVENT_started, on_pause_change, NULL) == SCS_RESULT_ok;
    if (!ok) {
        log_msg("[local-radio-linux] event registration failed");
        close_sock(g_sock);
        g_sock = SOCK_INVALID;
#ifdef _WIN32
        WSACleanup();
#endif
        return SCS_RESULT_generic_error;
    }
    p->register_for_channel(SCS_TELEMETRY_TRUCK_CHANNEL_world_placement, SCS_U32_NIL, SCS_VALUE_TYPE_dplacement,
                            SCS_TELEMETRY_CHANNEL_FLAG_no_value, on_world_placement, NULL);

    g_paused = true; // the game always starts paused
    g_have_pos = false;

    char msg[128];
    snprintf(msg, sizeof msg, "[local-radio-linux] sending telemetry to 127.0.0.1:%d (game %s)", port, g_game);
    log_msg(msg);
    return SCS_RESULT_ok;
}

EXPORT SCSAPI_VOID scs_telemetry_shutdown(void) {
    // Tell the server the game is gone so the web page can go idle.
    g_have_pos = false;
    g_paused = true;
    send_state("none");
    if (g_sock != SOCK_INVALID) {
        close_sock(g_sock);
        g_sock = SOCK_INVALID;
    }
#ifdef _WIN32
    WSACleanup();
#endif
    g_log = NULL;
}

#ifdef _WIN32
BOOL APIENTRY DllMain(HMODULE, DWORD, LPVOID) { return TRUE; }
#endif
