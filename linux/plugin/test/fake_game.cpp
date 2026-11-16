// Test harness: impersonates ETS2 far enough to drive the plugin without the game.
// Loads the plugin (.so via dlopen / .dll via LoadLibrary under Wine), calls
// scs_telemetry_init with fake params, then feeds pause/position/frame events.
//   fake_game <plugin> <x> <y> <z> [seconds]
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#  include <windows.h>
#  define LOAD(p) (void *)LoadLibraryA(p)
#  define SYM(h, n) (void *)GetProcAddress((HMODULE)(h), n)
static void sleep_ms(int ms) { Sleep(ms); }
#else
#  include <dlfcn.h>
#  include <unistd.h>
#  define LOAD(p) dlopen(p, RTLD_NOW)
#  define SYM(h, n) dlsym(h, n)
static void sleep_ms(int ms) { usleep(ms * 1000); }
#endif
#include "scssdk_telemetry.h"
#include "eurotrucks2/scssdk_eut2.h"
#include "common/scssdk_telemetry_truck_common_channels.h"

static scs_telemetry_event_callback_t ev_cb[8];
static scs_telemetry_channel_callback_t placement_cb;

static SCSAPI_VOID fake_log(const scs_log_type_t, const scs_string_t msg) { printf("game log: %s\n", msg); }
static SCSAPI_RESULT reg_event(const scs_event_t e, const scs_telemetry_event_callback_t cb, const scs_context_t) {
    if (e < 8) ev_cb[e] = cb;
    return SCS_RESULT_ok;
}
static SCSAPI_RESULT unreg_event(const scs_event_t) { return SCS_RESULT_ok; }
static SCSAPI_RESULT reg_channel(const scs_string_t name, const scs_u32_t, const scs_value_type_t,
                                 const scs_u32_t, const scs_telemetry_channel_callback_t cb, const scs_context_t) {
    if (strcmp(name, SCS_TELEMETRY_TRUCK_CHANNEL_world_placement) == 0) placement_cb = cb;
    return SCS_RESULT_ok;
}
static SCSAPI_RESULT unreg_channel(const scs_string_t, const scs_u32_t, const scs_value_type_t) { return SCS_RESULT_ok; }

typedef SCSAPI_RESULT (*init_t)(const scs_u32_t, const scs_telemetry_init_params_t *const);
typedef SCSAPI_VOID (*shutdown_t)(void);

int main(int argc, char **argv) {
    if (argc < 5) { fprintf(stderr, "usage: %s <plugin> <x> <y> <z> [seconds]\n", argv[0]); return 2; }
    void *h = LOAD(argv[1]);
    if (!h) { fprintf(stderr, "cannot load %s\n", argv[1]); return 1; }
    init_t init = (init_t)SYM(h, "scs_telemetry_init");
    shutdown_t shut = (shutdown_t)SYM(h, "scs_telemetry_shutdown");
    if (!init || !shut) { fprintf(stderr, "exports missing\n"); return 1; }

    scs_telemetry_init_params_v101_t p;
    memset(&p, 0, sizeof p);
    p.common.game_name = "Euro Truck Simulator 2";
    p.common.game_id = SCS_GAME_ID_EUT2;
    p.common.game_version = SCS_MAKE_VERSION(1, 18);
    p.common.log = fake_log;
    p.register_for_event = reg_event;
    p.unregister_from_event = unreg_event;
    p.register_for_channel = reg_channel;
    p.unregister_from_channel = unreg_channel;
    if (init(SCS_TELEMETRY_VERSION_1_01, &p) != SCS_RESULT_ok) { fprintf(stderr, "init failed\n"); return 1; }
    if (!placement_cb || !ev_cb[SCS_TELEMETRY_EVENT_frame_end]) { fprintf(stderr, "callbacks missing\n"); return 1; }

    scs_value_t v;
    memset(&v, 0, sizeof v);
    v.type = SCS_VALUE_TYPE_dplacement;
    v.value_dplacement.position.x = atof(argv[2]);
    v.value_dplacement.position.y = atof(argv[3]);
    v.value_dplacement.position.z = atof(argv[4]);
    int seconds = argc > 5 ? atoi(argv[5]) : 3;

    ev_cb[SCS_TELEMETRY_EVENT_started](SCS_TELEMETRY_EVENT_started, NULL, NULL);
    for (int i = 0; i < seconds * 60; ++i) { // ~60 fps
        placement_cb(SCS_TELEMETRY_TRUCK_CHANNEL_world_placement, SCS_U32_NIL, &v, NULL);
        ev_cb[SCS_TELEMETRY_EVENT_frame_end](SCS_TELEMETRY_EVENT_frame_end, NULL, NULL);
        v.value_dplacement.position.x += 0.5; // drive east a little
        sleep_ms(16);
    }
    shut();
    printf("fake game done\n");
    return 0;
}
