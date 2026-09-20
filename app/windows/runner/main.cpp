#include <flutter/dart_project.h>
#include <flutter/flutter_view_controller.h>
#include <windows.h>

#include "flutter_window.h"
#include "utils.h"

namespace {

// Shrink an initial window size so it always fits the desktop work area.
//
// Why this is needed: the runner scales the logical size by the monitor DPI
// before handing it to the OS. A hard-coded 1280x800 logical window becomes
// 1920x1200 physical at 150% scaling, which does not fit a 1707x1067 screen --
// the window opens partly off-screen and its right-hand side is cut off.
// Clamping (rather than just picking a smaller constant) keeps the app usable
// on any display, at any scaling.
Win32Window::Size ClampToWorkArea(Win32Window::Size size) {
  RECT work_area;
  if (!::SystemParametersInfo(SPI_GETWORKAREA, 0, &work_area, 0)) {
    return size;
  }

  // The runner scales by DPI, so convert the physical work area back to
  // logical units before comparing.
  UINT dpi = 96;
  HMODULE user32 = ::GetModuleHandleW(L"user32.dll");
  if (user32 != nullptr) {
    using GetDpiForSystemFn = UINT(WINAPI*)();
    auto get_dpi_for_system = reinterpret_cast<GetDpiForSystemFn>(
        ::GetProcAddress(user32, "GetDpiForSystem"));
    if (get_dpi_for_system != nullptr) {
      dpi = get_dpi_for_system();
    }
  }
  const double scale = static_cast<double>(dpi) / 96.0;

  // Leave room for the taskbar and the window frame.
  const int margin = 48;
  const int available_width = static_cast<int>(
      (work_area.right - work_area.left) / scale) - margin;
  const int available_height = static_cast<int>(
      (work_area.bottom - work_area.top) / scale) - margin;

  if (available_width > 0 &&
      size.width > static_cast<unsigned int>(available_width)) {
    size.width = static_cast<unsigned int>(available_width);
  }
  if (available_height > 0 &&
      size.height > static_cast<unsigned int>(available_height)) {
    size.height = static_cast<unsigned int>(available_height);
  }
  return size;
}

}  // namespace

int APIENTRY wWinMain(_In_ HINSTANCE instance, _In_opt_ HINSTANCE prev,
                      _In_ wchar_t *command_line, _In_ int show_command) {
  // Attach to console when present (e.g., 'flutter run') or create a
  // new console when running with a debugger.
  if (!::AttachConsole(ATTACH_PARENT_PROCESS) && ::IsDebuggerPresent()) {
    CreateAndAttachConsole();
  }

  // Initialize COM, so that it is available for use in the library and/or
  // plugins.
  ::CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

  flutter::DartProject project(L"data");

  std::vector<std::string> command_line_arguments =
      GetCommandLineArguments();

  project.set_dart_entrypoint_arguments(std::move(command_line_arguments));

  FlutterWindow window(project);
  Win32Window::Point origin(10, 10);
  Win32Window::Size size = ClampToWorkArea(Win32Window::Size(1280, 800));
  // Window title. The CJK characters are written as \u escapes on purpose, and
  // this whole file is kept ASCII-only on purpose.
  //
  // Why: MSVC reads .cpp sources using the *system code page* (936/GBK on a
  // Chinese Windows). This file is UTF-8 without a BOM, so raw CJK bytes get
  // mis-decoded. The symptoms are misleading: first "error C2001: newline in
  // constant" (looks like a syntax error), and once the literal is escaped,
  // "warning C4819 ... treated as error C2220" from any non-ASCII *comment*.
  // A UTF-8 BOM also fixes it, but a later re-save without the BOM would break
  // the build again with that same confusing error. Pure ASCII cannot break.
  //
  // The escapes below spell: iPod <music> <manager> in Simplified Chinese.
  if (!window.Create(L"iPod \u97F3\u4E50\u7BA1\u7406\u5668", origin, size)) {
    return EXIT_FAILURE;
  }
  window.SetQuitOnClose(true);

  ::MSG msg;
  while (::GetMessage(&msg, nullptr, 0, 0)) {
    ::TranslateMessage(&msg);
    ::DispatchMessage(&msg);
  }

  ::CoUninitialize();
  return EXIT_SUCCESS;
}
