<#
Click one button of a COMSOL desktop dialog. Two routes, tried in order:

1. UI Automation: find Button elements whose *normalized* label matches one of
   -Names by prefix. Chinese dialogs render "否(N)", English ones "No" or
   "No (&N)", so exact-name matching misses them (observed live: a
   "save changes?" dialog survived because "No" never matched "否(N)").
2. Native Win32 dialogs (#32770 - what SWT save-changes prompts are): enumerate
   child BUTTON windows and send BM_CLICK. SWT owner-drawn buttons are not
   reliably present in the UIA tree, but they are always real Win32 child
   windows, so this route covers them.

Before clicking, the button's own top-level window is raised: COMSOL can show
two modals at once (save-changes + "server busy"), and a click at the
coordinates of the covered one would land on whichever window is on top.

Safety: a button is only clicked when its *top-level window* belongs to one of
-Pids, or when that window is a COMSOL dialog (caption "COMSOL"). So this cannot
dismiss dialogs of unrelated applications.

Prints "CLICKED: <name>" on success, "NO-BUTTON" otherwise.
#>
param(
  [string]$Names = "No,Don't Save",
  [string]$Pids = ""
)

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class McpWin {
  public delegate bool EnumProc(IntPtr h, IntPtr l);

  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint flags);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr SendMessageW(IntPtr h, uint msg, IntPtr w, IntPtr l);

  public const uint BM_CLICK = 0x00F5;
  public const uint GA_ROOT = 2;

  // Keep callbacks alive: the native loop runs while we hold them.
  static EnumProc _keep;

  public static string ClassName(IntPtr h) {
    var sb = new StringBuilder(64);
    GetClassNameW(h, sb, sb.Capacity);
    return sb.ToString();
  }

  public static string WindowText(IntPtr h) {
    var sb = new StringBuilder(256);
    GetWindowTextW(h, sb, sb.Capacity);
    return sb.ToString();
  }

  public static IntPtr RootOf(IntPtr h) { return GetAncestor(h, GA_ROOT); }

  public static List<IntPtr> TopDialogs() {
    var found = new List<IntPtr>();
    _keep = (h, l) => {
      if (IsWindowVisible(h) && ClassName(h) == "#32770") found.Add(h);
      return true;
    };
    EnumWindows(_keep, IntPtr.Zero);
    _keep = null;
    return found;
  }

  public static List<IntPtr> ChildButtons(IntPtr parent) {
    var found = new List<IntPtr>();
    _keep = (h, l) => {
      if (IsWindowVisible(h) && ClassName(h) == "Button") found.Add(h);
      return true;
    };
    EnumChildWindows(parent, _keep, IntPtr.Zero);
    _keep = null;
    return found;
  }

  public static uint PidOf(IntPtr h) {
    uint pid;
    GetWindowThreadProcessId(h, out pid);
    return pid;
  }
}
"@

$allowed = @()
if ($Pids -ne "") { $allowed = @($Pids.Split(",") | ForEach-Object { [int]$_ }) }

function Normalize-Name([string]$name) {
  # "否(&N)" / "No (&N)" / " Don't  Save " -> "否" / "No" / "DontSave"
  $n = $name -replace '\(\s*.*?\s*\)', ''
  return ($n -replace '[\s&]', '')
}

function Match-Name([string]$raw) {
  $norm = Normalize-Name $raw
  if ($norm -eq "") { return $false }
  foreach ($name in $Names.Split(",")) {
    $target = Normalize-Name $name
    if ($target -ne "" -and $norm.StartsWith($target, [StringComparison]::OrdinalIgnoreCase)) {
      return $true
    }
  }
  return $false
}

function Test-Owner([IntPtr]$hwnd) {
  if ($allowed.Count -eq 0) { return $false }
  $wpid = [int][McpWin]::PidOf($hwnd)
  return ($allowed -contains $wpid)
}

function Raise-Window([IntPtr]$hwnd) {
  if ($hwnd -eq [IntPtr]::Zero) { return }
  [void][McpWin]::BringWindowToTop($hwnd)
  [void][McpWin]::SetForegroundWindow($hwnd)
  Start-Sleep -Milliseconds 400
}

# ---- route 1: UIA buttons -------------------------------------------------
$uiaRoot = [System.Windows.Automation.AutomationElement]::RootElement
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$cond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
  [System.Windows.Automation.ControlType]::Button)
$buttons = $uiaRoot.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
foreach ($button in $buttons) {
  $rect = $button.Current.BoundingRectangle
  if ($rect.Width -lt 30 -or $rect.Height -lt 8) { continue }
  $rawName = "$($button.Current.Name)"
  if (-not (Match-Name $rawName)) { continue }
  $native = [IntPtr]$button.Current.NativeWindowHandle
  $rootHwnd = if ($native -ne [IntPtr]::Zero) { [McpWin]::RootOf($native) } else { [IntPtr]::Zero }
  $names = @()
  $current = $button
  for ($i = 0; $i -lt 8; $i++) {
    $current = $walker.GetParent($current)
    if ($null -eq $current) { break }
    $names += "$($current.Current.Name)"
  }
  $owned = ($allowed -contains [int]$button.Current.ProcessId) -or (Test-Owner $rootHwnd)
  $isComsol = (($names -join '|') -match 'COMSOL')
  Write-Output "CANDIDATE uia name='$rawName' owned=$owned comsol=$isComsol"
  if (-not ($owned -or $isComsol)) { continue }
  Raise-Window $rootHwnd
  $rect = $button.Current.BoundingRectangle
  [void][McpWin]::SetCursorPos([int]($rect.X + $rect.Width / 2), [int]($rect.Y + $rect.Height / 2))
  Start-Sleep -Milliseconds 350
  [McpWin]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)
  Start-Sleep -Milliseconds 120
  [McpWin]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)
  Write-Output "CLICKED: $rawName"
  exit 0
}

# ---- route 2: native #32770 child BUTTON windows (BM_CLICK) ----------------
foreach ($dlg in [McpWin]::TopDialogs()) {
  $caption = [McpWin]::WindowText($dlg)
  $owned = Test-Owner $dlg
  $isComsol = $caption -match 'COMSOL'
  if (-not ($owned -or $isComsol)) { continue }
  foreach ($b in [McpWin]::ChildButtons($dlg)) {
    $label = [McpWin]::WindowText($b)
    if (-not (Match-Name $label)) { continue }
    Write-Output "CANDIDATE win32 name='$label' dialog='$caption' owned=$owned"
    Raise-Window $dlg
    [void][McpWin]::SendMessageW($b, [McpWin]::BM_CLICK, [IntPtr]::Zero, [IntPtr]::Zero)
    Write-Output "CLICKED: $label"
    exit 0
  }
}

Write-Output "NO-BUTTON"
exit 1

