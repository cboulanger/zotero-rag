# Zotero Reader API

This is a random collection of API details.

## Goto to PDF page

```javascript
{
    //let selectedItems = Zotero.getActiveZoteroPane().getSelectedItems();
    let selectedItemId = 95567 // selectedItems[0].id;
    let item = Zotero.Items.get(selectedItemId);
    await Zotero.FileHandlers.open(item, { pageNumber: 6 });
}
```