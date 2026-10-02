const progressTitles = {
  import: '正在导入质粒', export: '正在导出质粒', sync: '正在同步仓库', backup: '正在创建备份',
  verify: '正在校验备份', restore: '正在恢复仓库', move: '正在迁移原件'
};

window.plasmoraProgress = function (update) {
  const panel = document.querySelector('#operation-progress');
  if (update.phase === 'done') {
    panel.classList.add('hidden');
    document.querySelector('#cancel-operation').disabled = false;
    document.querySelector('#cancel-operation').textContent = '完成当前文件后取消';
    return;
  }
  panel.classList.remove('hidden');
  document.querySelector('#operation-title').textContent = progressTitles[update.phase] || '正在处理';
  document.querySelector('#operation-message').textContent = update.message || '正在准备…';
  const bar = document.querySelector('#operation-bar');
  if (update.total > 0) {
    bar.max = update.total;
    bar.value = update.current;
    document.querySelector('#operation-count').textContent = `${update.current} / ${update.total}`;
  } else {
    bar.removeAttribute('value');
    document.querySelector('#operation-count').textContent = '';
  }
};

document.querySelector('#cancel-operation').onclick = async () => {
  const button = document.querySelector('#cancel-operation');
  button.disabled = true;
  button.textContent = '已请求取消，等待当前文件完成…';
  try {
    await window.pywebview.api.cancel_operation();
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
};

let syncingRepository = false;
async function applyRepositoryUpdates(result, automatic = false) {
  if (result.updated.length) {
    for (const id of result.updated) delete state.previews[id];
    await loadPlasmids();
    renderList();
    if (result.updated.includes(state.selected)) {
      state.activeFeature = null;
      renderDetail();
    }
    toast(`${automatic ? '已自动同步' : '已同步'} ${result.updated.length} 个外部修改的质粒`);
  }
  if (result.errors.length) toast(`${result.errors.length} 项无法同步，请查看日志或手动同步仓库`);
}
async function syncRepository(quiet = false) {
  if (syncingRepository || !window.pywebview?.api?.sync_repository) {
    if (!quiet) toast('请在桌面程序中同步仓库');
    return;
  }
  syncingRepository = true;
  try {
    const result = await window.pywebview.api.sync_repository();
    if (result.cancelled) {
      toast('已停止同步');
      return;
    }
    if (result.error) throw new Error(result.error);
    await applyRepositoryUpdates(result);
    if (!result.updated.length && !result.errors.length && !quiet) {
      toast('仓库记录已是最新');
    }
  } catch (error) {
    toast(error.message);
  } finally {
    syncingRepository = false;
  }
}

document.querySelector('#sync-repository').onclick = () => syncRepository();
let receivingRepositoryUpdates = false;
let repositoryUpdateError = false;
async function receiveRepositoryUpdates() {
  if (receivingRepositoryUpdates || syncingRepository || !state.storage ||
      !window.pywebview?.api?.take_repository_updates ||
      !document.querySelector('#operation-progress').classList.contains('hidden')) return;
  receivingRepositoryUpdates = true;
  try {
    // Drain completed notifications only; this call never scans or parses files.
    const result = await window.pywebview.api.take_repository_updates();
    await applyRepositoryUpdates(result, true);
    repositoryUpdateError = false;
  } catch (error) {
    if (!repositoryUpdateError) toast('无法接收仓库更新，请手动同步仓库');
    repositoryUpdateError = true;
  } finally {
    receivingRepositoryUpdates = false;
  }
}
setInterval(receiveRepositoryUpdates, 2000);

async function showTrash() {
  try {
    const result = await api('/api/trash');
    const content = result.items.length ? result.items.map(item =>
      `<div class="trash-row"><div><strong>${esc(item.name)}</strong><small>${item.reason === 'replaced' ? '替换前的版本' : '已删除'} · ${esc(item.deletedAt.replace('T', ' '))}</small></div>` +
      `<div class="trash-actions"><button class="button" data-restore="${item.id}">恢复</button><button class="delete-link" data-purge="${item.id}">永久删除</button></div></div>`
    ).join('') : '<div class="no-results">回收站是空的</div>';
    openActionDialog('回收站', '删除的质粒和替换前的版本可以在这里恢复。',
      `<div class="trash-list">${content}</div><div class="dialog-buttons"><button class="button" id="close-trash">关闭</button></div>`, 'RECOVERY');
    document.querySelector('#close-trash').onclick = closeActionDialog;
    document.querySelectorAll('[data-restore]').forEach(button => button.onclick = async () => {
      button.disabled = true;
      try {
        const restored = await api(`/api/trash/${button.dataset.restore}/restore`, {method: 'POST', body: '{}'});
        await Promise.all([loadPlasmids(), loadGroups()]);
        render();
        toast(`已恢复“${restored.name}”`);
        await showTrash();
      } catch (error) {
        toast(error.message);
        button.disabled = false;
      }
    });
    document.querySelectorAll('[data-purge]').forEach(button => button.onclick = () => {
      const id = Number(button.dataset.purge);
      const item = result.items.find(row => row.id === id);
      openActionDialog('永久删除', `将永久删除“${item.name}”的可恢复副本。`,
        '<div class="dialog-buttons"><button class="button" id="cancel-purge">取消</button><button class="button danger-button" id="confirm-purge">永久删除</button></div>',
        'SECOND CONFIRMATION');
      document.querySelector('#cancel-purge').onclick = showTrash;
      document.querySelector('#confirm-purge').onclick = async () => {
        try {
          await api(`/api/trash/${id}`, {method: 'DELETE'});
          toast('已永久删除');
          await showTrash();
        } catch (error) {
          toast(error.message);
        }
      };
    });
  } catch (error) {
    toast(error.message);
  }
}
document.querySelector('#trash-tools').onclick = showTrash;

function confirmRestore(info) {
  openActionDialog('确认恢复备份',
    `备份创建于 ${info.createdAt || '未知时间'}，包含 ${info.count} 个质粒。当前仓库会先生成自动备份。`,
    '<div class="dialog-buttons"><button class="button" id="cancel-restore">取消</button><button class="button danger-button" id="confirm-restore">确认恢复</button></div>',
    'CONFIRM RESTORE');
  document.querySelector('#cancel-restore').onclick = closeActionDialog;
  document.querySelector('#confirm-restore').onclick = async () => {
    const button = document.querySelector('#confirm-restore');
    button.disabled = true;
    try {
      const result = await window.pywebview.api.restore_backup();
      if (result.cancelled) {
        toast('已取消恢复，原仓库保持可用');
        return;
      }
      if (result.error) throw new Error(result.error);
      state.previews = {};
      state.noteDrafts = {};
      state.selected = null;
      state.view = 'library';
      state.groupFilter = null;
      state.search = '';
      document.querySelector('#search').value = '';
      await Promise.all([loadSettings(), loadPlasmids(), loadGroups(), loadSynonymClusters()]);
      render();
      renderSynonymClusters();
      renderThemes();
      closeActionDialog();
      toast(`已恢复 ${result.count} 个质粒；旧仓库已自动备份`);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  };
}

function openEnhancedDataTools() {
  openActionDialog('备份与恢复', '完整备份包含数据库、分组、备注及所有托管的 .dna 文件。',
    '<div class="data-tools"><button class="button button-gold" id="create-backup">创建完整备份</button><button class="button" id="choose-restore">从文件恢复</button><button class="button" id="show-rollback">查看自动备份</button></div>',
    'LOCAL DATA');
  document.querySelector('#create-backup').onclick = async event => {
    if (!window.pywebview?.api?.backup_library) return toast('请在桌面程序中备份');
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await window.pywebview.api.backup_library();
      if (result.cancelled) return toast('已取消备份');
      if (result.error) throw new Error(result.error);
      closeActionDialog();
      toast(`已备份 ${result.count} 个质粒`);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  };
  document.querySelector('#choose-restore').onclick = async () => {
    if (!window.pywebview?.api?.choose_backup_for_restore) return toast('请在桌面程序中恢复');
    try {
      const info = await window.pywebview.api.choose_backup_for_restore();
      if (info.cancelled) return;
      if (info.error) throw new Error(info.error);
      confirmRestore(info);
    } catch (error) {
      toast(error.message);
    }
  };
  document.querySelector('#show-rollback').onclick = showRollbackBackups;
}

async function showRollbackBackups() {
  try {
    const result = await api('/api/rollback');
    const content = result.items.length ? result.items.map(item =>
      `<div class="trash-row"><div><strong>${esc(item.name)}</strong><small>${(item.size / 1024 / 1024).toFixed(1)} MB</small></div><button class="button" data-rollback="${esc(item.name)}">选择恢复</button></div>`
    ).join('') : '<div class="no-results">尚无恢复前自动备份</div>';
    openActionDialog('恢复前自动备份', '每次恢复旧备份前，程序会保存当前仓库。',
      `<div class="trash-list">${content}</div><div class="dialog-buttons"><button class="button" id="back-to-backups">返回</button></div>`,
      'ROLLBACK');
    document.querySelector('#back-to-backups').onclick = openEnhancedDataTools;
    document.querySelectorAll('[data-rollback]').forEach(button => button.onclick = async () => {
      try {
        const info = await window.pywebview.api.choose_rollback_for_restore(button.dataset.rollback);
        if (info.error) throw new Error(info.error);
        confirmRestore(info);
      } catch (error) {
        toast(error.message);
      }
    });
  } catch (error) {
    toast(error.message);
  }
}

document.querySelector('#backup-tools').onclick = openEnhancedDataTools;
api('/api/about').then(info => {
  document.querySelector('#about-version').textContent = `版本 ${info.version}`;
}).catch(() => {
  document.querySelector('#about-version').textContent = '版本未知';
});
document.querySelector('#open-log-folder').onclick = async () => {
  try {
    const result = await window.pywebview.api.open_log_folder();
    if (result.error) throw new Error(result.error);
  } catch (error) {
    toast(error.message);
  }
};
document.querySelector('#open-project-page').onclick = async () => {
  try {
    const result = await window.pywebview.api.open_project_page();
    if (result.error) throw new Error(result.error);
  } catch (error) {
    toast(error.message);
  }
};
