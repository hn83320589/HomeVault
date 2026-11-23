"""例外階層。每種錯誤對應一個 CLI exit code，讓排程器可以判斷結果。"""


class HomeVaultError(Exception):
    """所有 HomeVault 預期內錯誤的基底類別。"""

    exit_code = 1


class UsageError(HomeVaultError):
    """指令參數錯誤。"""

    exit_code = 2


class WrongPasswordError(HomeVaultError):
    """密碼錯誤。拋出時保證尚未對備份庫做任何寫入。"""

    exit_code = 10


class RepositoryLockedError(HomeVaultError):
    """備份庫正被另一個行程（可能在另一台電腦上）使用。"""

    exit_code = 11


class RepositoryUnavailableError(HomeVaultError):
    """備份庫無法存取，例如外接硬碟被拔除。"""

    exit_code = 12


class IntegrityError(HomeVaultError):
    """資料驗證失敗：解密失敗、雜湊不符或格式損毀。"""

    exit_code = 13


class RepositoryNotFoundError(HomeVaultError):
    """指定的路徑不是 HomeVault 備份庫。"""

    exit_code = 1


class NotFoundError(HomeVaultError):
    """找不到快照或快照內的路徑。"""

    exit_code = 1
