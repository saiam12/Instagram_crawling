"use strict";
// Shared last-workspace storage for file exploration and video planning.
window.CollectorWorkspaceCache = (() => {
  const databases = new Map();
  async function action(name, operation, value) {
    if (!databases.has(name)) databases.set(name, new Promise((resolve, reject) => {
      const request = indexedDB.open(name, 1);
      request.onupgradeneeded = () => request.result.createObjectStore("workspace");
      request.onerror = () => { databases.delete(name); reject(request.error); };
      request.onblocked = () => { databases.delete(name); reject(new Error("다른 작업 탭을 닫고 다시 시도하세요.")); };
      request.onsuccess = () => {
        const database = request.result;
        database.onversionchange = () => { database.close(); databases.delete(name); };
        resolve(database);
      };
    }));
    const database = await databases.get(name);
    return new Promise((resolve, reject) => {
      const transaction = database.transaction("workspace", operation === "get" ? "readonly" : "readwrite");
      const store = transaction.objectStore("workspace");
      const request = operation === "put" ? store.put(value, "last") : operation === "delete" ? store.delete("last") : store.get("last");
      transaction.oncomplete = () => resolve(request.result);
      transaction.onerror = () => reject(transaction.error || request.error);
      transaction.onabort = () => reject(transaction.error || new Error("브라우저 저장이 취소되었습니다."));
    });
  }
  return {action};
})();
