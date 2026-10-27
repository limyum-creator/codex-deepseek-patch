# Native Windows view for update_progress.py. The controller owns every update.
param(
    [Parameter(Mandatory=$true)][string]$StateDirectory,
    [Parameter(Mandatory=$true)][string]$PythonExecutable,
    [Parameter(Mandatory=$true)][string]$ControllerPath,
    [switch]$SelfTest,
    [ValidateSet('start','launch')][string]$SelfTestAction,
    [switch]$SelfTestDismiss,
    [string]$SelfTestStaleId
)
$ErrorActionPreference = 'Stop'
foreach ($path in @($StateDirectory, $PythonExecutable, $ControllerPath)) {
    if (-not [IO.Path]::IsPathRooted($path)) { exit 2 }
}
$StateDirectory = [IO.Path]::GetFullPath($StateDirectory)
$PythonExecutable = [IO.Path]::GetFullPath($PythonExecutable)
$ControllerPath = [IO.Path]::GetFullPath($ControllerPath)
$StateFile = [IO.Path]::Combine($StateDirectory, 'progress.json')
if (-not [IO.File]::Exists($PythonExecutable) -or -not [IO.File]::Exists($ControllerPath)) { exit 2 }
if ([Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') { exit 2 }
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$script:runId = $null
$script:state = $null
$phases = @('current','available','checking','downloading','checking_package','building','verifying','waiting_for_exit',
            'waiting_for_idle','close_failed','recovering','rolled_back','closing','installing','activating','completed','failed')

function Quote-Argument([string]$Value) {
    # Windows CRT argv quoting; no shell interprets these arguments.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Send-Action([string]$Action) {
    # Only UI-generated fixed actions cross this boundary. Never run state data.
    if ($Action -notin @('start','launch','dismiss','cancel') -or -not $script:runId) { return }
    if ($Action -eq 'start' -and $script:state.can_start -ne $true) { return }
    if ($Action -eq 'launch' -and $script:state.can_launch -ne $true) { return }
    $arguments = @($ControllerPath, '--state-dir', $StateDirectory, '--action', $Action, '--id', $script:runId)
    if ($Action -eq 'dismiss') { $arguments += @('--phase', [string]$script:state.phase) }
    try {
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = $PythonExecutable
        $info.Arguments = ($arguments | ForEach-Object { Quote-Argument $_ }) -join ' '
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.WindowStyle = 'Hidden'
        $process = [Diagnostics.Process]::Start($info)
        if ($process) { $process.Dispose() }
    } catch {
        # A vanished controller must not interrupt a running update.
    }
}

function Short-Text($Value, [int]$Limit) {
    if ($Value -isnot [string]) { return '' }
    return $Value.Substring(0, [Math]::Min($Value.Length, $Limit))
}

$form = New-Object Windows.Forms.Form
$form.Text = 'Codex 模型补丁更新'
if(Test-Path (Join-Path $StateDirectory 'settings-ui/brand.ico')){$form.Icon=[Drawing.Icon]::new((Join-Path $StateDirectory 'settings-ui/brand.ico'))}
$form.ClientSize = New-Object Drawing.Size(560, 300)
$form.MinimumSize = New-Object Drawing.Size(540, 338)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedSingle'
$form.MaximizeBox = $false
$form.TopMost = $false
$form.ShowInTaskbar = $true
$form.BackColor = [Drawing.Color]::FromArgb(25, 27, 31)
$form.ForeColor = [Drawing.Color]::FromArgb(239, 241, 244)
$form.Font = New-Object Drawing.Font('Microsoft YaHei UI', 10)

$brand = New-Object Windows.Forms.Label
$brand.Text = 'CODEX · 模型补丁'
$brand.Location = New-Object Drawing.Point(28, 22)
$brand.Size = New-Object Drawing.Size(490, 24)
$brand.ForeColor = [Drawing.Color]::FromArgb(151, 160, 174)
$form.Controls.Add($brand)
$title = New-Object Windows.Forms.Label
$title.Text = '正在检查更新状态…'
$title.Location = New-Object Drawing.Point(26, 55)
$title.Size = New-Object Drawing.Size(505, 38)
$title.Font = New-Object Drawing.Font('Microsoft YaHei UI', 16, [Drawing.FontStyle]::Bold)
$form.Controls.Add($title)
$message = New-Object Windows.Forms.Label
$message.Location = New-Object Drawing.Point(28, 105)
$message.Size = New-Object Drawing.Size(500, 66)
$message.ForeColor = [Drawing.Color]::FromArgb(194, 201, 210)
$message.Text = '可继续使用其他程序，关闭此窗口不会中断后台更新。'
$form.Controls.Add($message)
$progress = New-Object Windows.Forms.ProgressBar
$progress.Location = New-Object Drawing.Point(28, 184)
$progress.Size = New-Object Drawing.Size(500, 8)
$progress.Style = 'Marquee'
$progress.MarqueeAnimationSpeed = 28
$form.Controls.Add($progress)
$percent = New-Object Windows.Forms.Label
$percent.Location = New-Object Drawing.Point(28, 201)
$percent.Size = New-Object Drawing.Size(500, 22)
$percent.ForeColor = $brand.ForeColor
$form.Controls.Add($percent)

$ignore = New-Object Windows.Forms.Button
$ignore.Text = '忽略'
$ignore.Location = New-Object Drawing.Point(28, 244)
$ignore.Size = New-Object Drawing.Size(90, 32)
$ignore.FlatStyle = 'Flat'
$ignore.BackColor = [Drawing.Color]::FromArgb(43, 48, 56)
$ignore.ForeColor = [Drawing.Color]::FromArgb(239, 241, 244)
$ignore.Add_Click({ $form.Close() })
$form.Controls.Add($ignore)
$start = New-Object Windows.Forms.Button
$start.Text = '开始更新'
$start.Location = New-Object Drawing.Point(308, 244)
$start.Size = New-Object Drawing.Size(104, 32)
$start.Enabled = $false
$start.FlatStyle = 'Flat'
$start.BackColor = [Drawing.Color]::FromArgb(43, 48, 56)
$start.ForeColor = [Drawing.Color]::FromArgb(239, 241, 244)
$start.Add_Click({ Send-Action $(if($script:state.can_cancel){'cancel'}else{'start'}); $start.Enabled = $false })
$form.Controls.Add($start)
$launch = New-Object Windows.Forms.Button
$launch.Text = '启动 Codex'
$launch.Location = New-Object Drawing.Point(424, 244)
$launch.Size = New-Object Drawing.Size(104, 32)
$launch.Enabled = $false
$launch.FlatStyle = 'Flat'
$launch.BackColor = [Drawing.Color]::FromArgb(43, 48, 56)
$launch.ForeColor = [Drawing.Color]::FromArgb(239, 241, 244)
$launch.Add_Click({ Send-Action 'launch'; $launch.Enabled = $false })
$form.Controls.Add($launch)

function Refresh-State {
    try {
        $info = New-Object IO.FileInfo($StateFile)
        if (-not $info.Exists -or $info.Length -gt 65536) { return }
        $next = [IO.File]::ReadAllText($StateFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
        if($next.display -and $next.display.id -eq $next.id -and $next.display.edition -eq $next.edition){foreach($key in $next.display.PSObject.Properties){$next | Add-Member -NotePropertyName $key.Name -NotePropertyValue $key.Value -Force}}
        if ($next.phase -notin $phases) { return }
        if ($next.format -ne 1 -or $next.id -isnot [string] -or $next.id.Length -gt 128 -or -not $next.id) { return }
        if ($script:runId -and $next.id -ne $script:runId) { $form.Close(); return }
        $script:runId = $next.id
        $script:state = $next
        $title.Text = Short-Text $next.title 100
        $message.Text = Short-Text $next.detail 500
        $start.Enabled = $next.can_start -eq $true -or $next.can_cancel -eq $true
        $start.Text = if($next.can_cancel){'取消等待'}else{'开始更新'}
        $launch.Enabled = $next.can_launch -eq $true
        if ($next.phase -in @('current','completed','failed','rolled_back','close_failed')) {
            $progress.MarqueeAnimationSpeed = 0
            $progress.Style = 'Continuous'
            $progress.Value = if ($next.phase -eq 'completed') { 100 } else { 0 }
            $percent.Text = ''
            return
        }
        $progress.MarqueeAnimationSpeed = 28
        if ($null -ne $next.completed -and $null -ne $next.total -and
            ($next.completed -is [int] -or $next.completed -is [long]) -and
            ($next.total -is [int] -or $next.total -is [long])) {
            $completed = [double]$next.completed
            $total = [double]$next.total
            if (-not [double]::IsNaN($completed) -and -not [double]::IsInfinity($completed) -and
                -not [double]::IsNaN($total) -and -not [double]::IsInfinity($total) -and $total -gt 0 -and $completed -ge 0) {
                $progress.Style = 'Continuous'
                $progress.Value = [int][Math]::Min(100, 100 * $completed / $total)
                $percent.Text = [string][Math]::Floor(100*$completed/$total) + '% · ' + [string]$next.completed + ' / ' + [string]$next.total
                return
            }
        }
        $progress.Style = 'Marquee'
        $percent.Text = ''
    } catch {
        # Atomic state replacement or a stopped controller: retain the view.
    }
}
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 350
$timer.Add_Tick({ Refresh-State })
$form.Add_Shown({ Refresh-State; $timer.Start() })
$form.Add_FormClosing({ Send-Action 'dismiss'; $timer.Stop() })
$form.Add_FormClosed({ $timer.Dispose() })
if ($SelfTest -or $SelfTestAction -or $SelfTestDismiss -or $SelfTestStaleId) {
    # Exercise real controls/events without Show(), handles or a message loop.
    Refresh-State
    if (-not $script:state) { $form.Dispose(); exit 3 }
    if ($SelfTestStaleId) { $script:runId = $SelfTestStaleId; Refresh-State }
    if ($SelfTestAction) {
        $button = if ($SelfTestAction -eq 'start') { $start } else { $launch }
        if ($button.Enabled) {
            $click = [Windows.Forms.Control].GetMethod('OnClick',
                [Reflection.BindingFlags]'Instance,NonPublic')
            [void]$click.Invoke($button, @([EventArgs]::Empty))
        }
    }
    if ($SelfTestDismiss) {
        $closing = [Windows.Forms.Form].GetMethod('OnFormClosing',
            [Reflection.BindingFlags]'Instance,NonPublic')
        $event = New-Object Windows.Forms.FormClosingEventArgs([Windows.Forms.CloseReason]::UserClosing, $false)
        # Reflection's object[] binder does not unwrap a New-Object PSObject.
        [object[]]$closingArguments = @($event.PSObject.BaseObject)
        [void]$closing.Invoke($form, $closingArguments)
    }
    $primary = if ($script:state.phase -eq 'completed') { $launch } else { $start }
    $snapshot = @{phase=$script:state.phase; form_visible=$form.Visible; form_disposed=$form.IsDisposed; form_title=$form.Text;
        title=$title.Text; detail=$message.Text; primary_text=$primary.Text; primary_enabled=$primary.Enabled;
        start_text=$start.Text; start_enabled=$start.Enabled; launch_text=$launch.Text; launch_enabled=$launch.Enabled;
        progress_style=[string]$progress.Style; progress_value=$progress.Value; work_text=$percent.Text}
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
    [Console]::WriteLine(($snapshot | ConvertTo-Json -Compress))
    $timer.Dispose()
    $form.Dispose()
    exit 0
}
# A normal, modeless application window; no global modal dialog or worker wait.
$form.Show()
[Windows.Forms.Application]::Run($form)
$form.Dispose()
