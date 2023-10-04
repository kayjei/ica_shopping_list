# ica_shopping_list
HomeAssistant integration to ICA shopping list

Install as custom component, manually or using HACS.<br>
You need to have a valid ICA account and a password (4-6 digits)<br><br>
You then need to specify store sorting for sorting your list items (0 = no sort order) 

Add in configuration.yaml:

```
ica_shopping_list:
  username: !secret ica_username
  listname: My shopping list
  password: !secret ica_pw
  storesorting: 0
```

In your secrets.yaml add:
```
ica_username: [USERNAME]
ica_pw: [4-6 DIGIT PASSWORD]
```



```listname``` is case sensitive.<br>
If the list is not found, it will be created. Space and å, ä, ö is valid.