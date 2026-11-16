# Builds the Local Radio UDP telemetry plugin for both targets.
#   make            -> build/local-radio-linux.dll + build/local-radio-linux.so
#   make dll | so   -> just one of them
SDK      := ../../plugin/scs_sdk/include
SRC      := local_radio_udp.cpp
OUT      := build
CXXFLAGS := -O2 -Wall -Wextra -Wno-unused-parameter -fno-exceptions -fno-rtti -I$(SDK)

MINGW_CXX ?= x86_64-w64-mingw32-g++
CXX       ?= g++

all: dll so

dll: $(OUT)/local-radio-linux.dll
so:  $(OUT)/local-radio-linux.so

$(OUT):
	mkdir -p $(OUT)

# Static runtime: the DLL must not depend on libstdc++/libgcc DLLs that the game folder lacks.
$(OUT)/local-radio-linux.dll: $(SRC) | $(OUT)
	$(MINGW_CXX) $(CXXFLAGS) -shared -static -static-libgcc -static-libstdc++ -s -o $@ $< -lws2_32

$(OUT)/local-radio-linux.so: $(SRC) | $(OUT)
	$(CXX) $(CXXFLAGS) -shared -fPIC -fvisibility=hidden -static-libgcc -static-libstdc++ -s -o $@ $<

clean:
	rm -rf $(OUT)

.PHONY: all dll so clean
