# Dog CEO image service: quick reference

The service hands out photos of dogs and the list of breeds it knows. Everything is read-only, needs no
account and no key, and answers with JSON. All addresses below hang off one base URL:

    https://dog.ceo/api

Every reply is an object with a `status` field (the text "success" when things went well) and a `message`
field that carries the actual answer.

## Listing every breed

Send a GET request to `/breeds/list/all`. It takes no parameters.

The `message` field is an object whose keys are main breed names. Each value is a list of sub-breed names,
and that list is empty when the breed has no sub-breeds. Sample (shortened):

    {"message": {"affenpinscher": [], "african": ["wild"], "australian": ["kelpie", "shepherd"]}, "status": "success"}

## One random picture from any breed

Send a GET request to `/breeds/image/random`. It takes no parameters. The `message` field holds the web
address of a single randomly chosen photo. Sample:

    {"message": "https://images.dog.ceo/breeds/brabancon/n02112706_449.jpg", "status": "success"}

## One random picture of a chosen breed

Send a GET request to `/breed/{breed}/images/random`, replacing `{breed}` with a main breed name such as
`hound`. The `breed` value is required and is part of the path. The `message` field holds the web address of
one random photo of that breed. Sample for `hound`:

    {"message": "https://images.dog.ceo/breeds/hound-basset/n02088238_9922.jpg", "status": "success"}
