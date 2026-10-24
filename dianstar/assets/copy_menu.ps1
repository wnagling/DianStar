# Runtime resource: Windows Forms context menu; input contains cursor coordinates only.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'
using System.Drawing;
using System.Windows.Forms;
using System;
using System.Runtime.InteropServices;
public class DianStarCopyMenu : ContextMenuStrip {
    protected override CreateParams CreateParams {
        get {
            CreateParams parameters = base.CreateParams;
            // A popup is a tool window, not a standalone taskbar application.
            parameters.ExStyle = (parameters.ExStyle | 0x00000080) & ~0x00040000;
            return parameters;
        }
    }
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int key);
    readonly Timer dismissTimer = new Timer();
    bool leftWasDown;
    public DianStarCopyMenu() {
        AutoClose = true;
        dismissTimer.Interval = 30;
        dismissTimer.Tick += delegate {
            if ((GetAsyncKeyState(0x1B) & 0x8000) != 0) {
                Close(ToolStripDropDownCloseReason.Keyboard);
                return;
            }
            bool leftDown = (GetAsyncKeyState(0x01) & 0x8000) != 0;
            if (leftDown && !leftWasDown && !Bounds.Contains(Cursor.Position))
                Close(ToolStripDropDownCloseReason.AppClicked);
            leftWasDown = leftDown;
        };
    }
    protected override void OnOpened(EventArgs e) {
        base.OnOpened(e);
        SetForegroundWindow(Handle);
        Focus();
        leftWasDown = (GetAsyncKeyState(0x01) & 0x8000) != 0;
        dismissTimer.Start();
    }
    protected override void OnClosed(ToolStripDropDownClosedEventArgs e) {
        dismissTimer.Stop();
        base.OnClosed(e);
    }
    protected override void Dispose(bool disposing) {
        if (disposing) dismissTimer.Dispose();
        base.Dispose(disposing);
    }
}
public class DianStarMenuColors : ProfessionalColorTable {
    public override Color ToolStripDropDownBackground { get { return Color.FromArgb(43,43,43); } }
    public override Color MenuBorder { get { return Color.FromArgb(60,60,60); } }
    public override Color MenuItemSelected { get { return Color.FromArgb(62,62,62); } }
    public override Color MenuItemBorder { get { return Color.FromArgb(62,62,62); } }
    public override Color ImageMarginGradientBegin { get { return ToolStripDropDownBackground; } }
    public override Color ImageMarginGradientMiddle { get { return ToolStripDropDownBackground; } }
    public override Color ImageMarginGradientEnd { get { return ToolStripDropDownBackground; } }
}
public class DianStarMenuRenderer : ToolStripProfessionalRenderer {
    public DianStarMenuRenderer() : base(new DianStarMenuColors()) { RoundedEdges = true; }
    protected override void OnRenderItemText(ToolStripItemTextRenderEventArgs e) {
        e.TextRectangle = new Rectangle(e.TextRectangle.X, 0, e.TextRectangle.Width, e.Item.Height);
        e.TextFormat = (e.TextFormat & ~TextFormatFlags.Bottom) |
            TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine;
        base.OnRenderItemText(e);
    }
    protected override void OnRenderItemImage(ToolStripItemImageRenderEventArgs e) {
        Rectangle rect = e.ImageRectangle;
        rect.Y = (e.Item.Height - rect.Height) / 2;
        base.OnRenderItemImage(new ToolStripItemImageRenderEventArgs(e.Graphics, e.Item, e.Image, rect));
    }
}
'@
[System.Windows.Forms.Application]::EnableVisualStyles()
[Console]::WriteLine('ready')
[Console]::Out.Flush()
while ($null -ne ($request = [Console]::In.ReadLine())) {
$position = $request | ConvertFrom-Json
$context = [System.Windows.Forms.ApplicationContext]::new()
$menu = [DianStarCopyMenu]::new()
$menu.Renderer = [DianStarMenuRenderer]::new()
$menu.Renderer.RoundedEdges = $true
$menu.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', 10)
$menu.ForeColor = [System.Drawing.Color]::FromArgb(240,240,240)
$menu.BackColor = [System.Drawing.Color]::FromArgb(43,43,43)
$menu.Padding = [System.Windows.Forms.Padding]::new(4)
$item = [System.Windows.Forms.ToolStripMenuItem]::new('复制')
$item.ShortcutKeys = [System.Windows.Forms.Keys]::Control -bor [System.Windows.Forms.Keys]::C
$item.ShowShortcutKeys = $false
$item.AutoSize = $false
$item.Size = [System.Drawing.Size]::new(130,38)
$item.ForeColor = $menu.ForeColor
$item.TextAlign = [System.Drawing.ContentAlignment]::MiddleLeft
$item.ImageAlign = [System.Drawing.ContentAlignment]::MiddleLeft
$icon = [System.Drawing.Bitmap]::new(20,20)
$graphics = [System.Drawing.Graphics]::FromImage($icon)
$pen = [System.Drawing.Pen]::new($menu.ForeColor,1.3)
$graphics.DrawRectangle($pen,3,6,10,11)
$graphics.DrawRectangle($pen,7,2,10,11)
$graphics.Dispose()
$pen.Dispose()
$item.Image = $icon
[void]$menu.Items.Add($item)
$script:choice = 'cancel'
$item.add_Click({ $script:choice = 'copy' })
$menu.add_Closed({
    param($sender, $eventArgs)
    if ($script:choice -ne 'copy' -and $eventArgs.CloseReason -in @(
        [System.Windows.Forms.ToolStripDropDownCloseReason]::AppClicked,
        [System.Windows.Forms.ToolStripDropDownCloseReason]::AppFocusChange)) {
        $script:choice = 'outside'
    }
    $context.ExitThread()
})
$menu.add_Opened({
    [Console]::WriteLine('shown')
    [Console]::Out.Flush()
})
# Show only after the Windows message loop is running, so menu input is registered.
$openTimer = [System.Windows.Forms.Timer]::new()
$openTimer.Interval = 1
$openTimer.add_Tick({
    $openTimer.Stop()
    $menu.Show([System.Drawing.Point]::new([int]$position.x,[int]$position.y))
})
try {
    $openTimer.Start()
    [System.Windows.Forms.Application]::Run($context)
    [Console]::WriteLine($script:choice)
    [Console]::Out.Flush()
} finally {
    $menu.Dispose()
    $openTimer.Dispose()
    $icon.Dispose()
    $context.Dispose()
}
}
