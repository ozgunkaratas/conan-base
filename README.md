your conan recipe, extending conan-base, can be executed after you initialize conan within your directory. In this directory, in your conan-base folder lives your conanfile.py, and in your project folder lives your normal conanfile.py
An example

- cd C:\thisIsMyDirectory
- conan create conan-base
- conan list "cmake-base/*"
