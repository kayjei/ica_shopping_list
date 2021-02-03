# ica_shopping_list
HomeAssistant integration to ICA shopping list

Install as custom component, manually or using HACS.<br>
You need to have a valid ICA account and a password (6 digits)<br><br>
Add in configuration.yaml:

```
ica_shopping_list:
  username: ICA-USERNAME
  listname: My shopping list 
  password: ICA PASSWORD
```

```listname``` is case sensitive.<br>
If the list is not found, it will be created. Space and å, ä, ö is valid.
