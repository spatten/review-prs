ObjC.import('AppKit');
function run() {
    const board = $.NSPasteboard.generalPasteboard;
    const change = Number(board.changeCount);
    const items = board.pasteboardItems;
    const result = [];
    if (!items || items.isNil()) return JSON.stringify(result);
    for (let i = 0; i < Number(items.count); i++) {
        const item = items.objectAtIndex(i);
        for (const type of ['public.utf8-plain-text', 'public.html']) {
            const value = item.stringForType(type);
            if (value && !value.isNil()) result.push(ObjC.unwrap(value));
        }
    }
    if (Number(board.changeCount) !== change) throw Error('Clipboard changed while reading; try again.');
    return JSON.stringify(result);
}
