"""Double-click entry point; kept separate from CLI / scheduler startup."""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    try:
        from app.housing.share_gui import main
        main()
    except Exception:
        import tkinter as tk
        from tkinter import messagebox
        work = ROOT / "data/share-tool"
        work.mkdir(parents=True, exist_ok=True)
        (work / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("工具启动失败", "请查看 D:\\HousingBot\\data\\share-tool\\startup-error.log", parent=root)
        root.destroy()
